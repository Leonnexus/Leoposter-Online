# ==========================================
import eventlet
eventlet.monkey_patch()
# ==========================================

import os
import random
import string
import uuid
from datetime import datetime
from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, emit
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from banco_palavras import BANCO_PALAVRAS

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///leoposter.db')
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, poolclass=NullPool)

EMOJIS_DISPONIVEIS = [
    "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐨", "🐯", 
    "🦁", "🐮", "🐷", "🐸", "🐵", "🐙", "🐢", "🦕", "🦞", "🦄", 
    "👽", "🤖", "👻", "👾", "🦉", "🦇", "🐺", "🐗", "🐴", "🐝", 
    "🐛", "🦋", "🐌", "🐞", "🐜", "🐠", "🐬", "🐳", "🦈", "🐊", 
    "🐅", "🐆", "🦓", "🦍", "🐘", "🦛", "🦏", "🐪", "🦒", "🦘"
]

def obter_ranking_dinamico(tipo):
    try:
        df = pd.read_sql_table("Rodadas", engine)
        df = df[df["Dia"] != "SOMATÓRIA"].copy()
        if df.empty: return []

        df["Data_Real"] = pd.to_datetime(df["Dia"], format="%d/%m/%Y", errors="coerce")
        hoje = pd.Timestamp(datetime.now().date())

        if tipo == "Dia": df = df[df["Data_Real"] == hoje]
        elif tipo == "Semana": df = df[(df["Data_Real"].dt.isocalendar().week == hoje.isocalendar().week) & (df["Data_Real"].dt.isocalendar().year == hoje.isocalendar().year)]
        
        if df.empty: return []

        ranking = []
        for nome, group in df.groupby("Nome"):
            pts = group["Pontos na Rodada"].sum()
            rodadas = len(group)
            media = round(pts / rodadas, 2) if rodadas > 0 else 0
            ranking.append({"Jogador": nome, "Pontuação Total": int(pts), "Média de Pontos": float(media)})

        ranking.sort(key=lambda x: (x["Média de Pontos"], x["Pontuação Total"]), reverse=True)
        return ranking
    except ValueError: return []

def carregar_dados_sala_do_banco(sala):
    try:
        df_palavras = pd.read_sql_table("Palavras_Usadas", engine)
        sala['palavras_usadas'] = set(df_palavras["Palavra"].dropna().astype(str).tolist())
    except ValueError: pass

    try:
        df_eventos = pd.read_sql_table("Estado_Eventos", engine)
        if not df_eventos.empty:
            sala['acumulado_caos'] = int(df_eventos["Acumulado_Caos"].iloc[0])
            sala['acumulado_trapaca'] = int(df_eventos["Acumulado_Trapaca"].iloc[0])
    except ValueError: pass

def salvar_banco_dados(pontos_da_rodada, placar, id_partida, detalhes_rodada, historico_impostores, palavras_usadas, acumulado_caos, acumulado_trapaca):
    dia_hoje = datetime.now().strftime("%d/%m/%Y")
    try:
        df = pd.read_sql_table("Rodadas", engine)
        df = df[df["Dia"] != "SOMATÓRIA"]
    except ValueError:
        df = pd.DataFrame(columns=["ID_Partida", "Nome", "Papel", "Votos_Recebidos", "Acertos_Detetive", "Votos_Efetuados", "Pontos na Rodada", "Placar Geral da Partida", "Dia"])
        
    novas_linhas = [{
        "ID_Partida": id_partida, "Nome": j, "Papel": detalhes_rodada[j]["Papel"],
        "Votos_Recebidos": detalhes_rodada[j]["Votos_Recebidos"], "Acertos_Detetive": detalhes_rodada[j]["Acertos_Detetive"],
        "Votos_Efetuados": detalhes_rodada[j]["Votos_Efetuados"], "Pontos na Rodada": int(pts),
        "Placar Geral da Partida": int(placar[j]), "Dia": str(dia_hoje)
    } for j, pts in pontos_da_rodada.items()]
    
    df = pd.concat([df, pd.DataFrame(novas_linhas)], ignore_index=True)
    df["Dia"] = df["Dia"].astype(str)
    
    df_hoje = df[df["Dia"] == str(dia_hoje)]
    soma = df_hoje.groupby("Nome")["Pontos na Rodada"].sum().reset_index()
    soma["Nome"] = soma["Nome"].apply(lambda x: f"TOTAL {x}")
    soma["Dia"] = "SOMATÓRIA"
    df_final_rodadas = pd.concat([df, soma], ignore_index=True)
    
    df_raw = df[df["Dia"] != "SOMATÓRIA"].copy()
    resumo_list, analytics_list = [], []
    for nome, group in df_raw.groupby("Nome"):
        partidas = group["ID_Partida"].nunique(); rodadas = len(group); pts_totais = group["Pontos na Rodada"].sum()
        media = pts_totais / rodadas if rodadas > 0 else 0; vitorias = sum(group["Pontos na Rodada"] > 0)
        resumo_list.append({"Jogador": nome, "Partidas Jogadas": int(partidas), "Rodadas Jogadas": int(rodadas), "Pontuação Total": int(pts_totais), "Média de Pontos": round(float(media), 2), "Vitórias em Rodadas": int(vitorias)})
        
        impostor_df = group[group["Papel"] == "Impostor"]
        vz_imp = len(impostor_df); fugas = len(impostor_df[impostor_df["Pontos na Rodada"] > 0])
        taxa_fuga = (fugas / vz_imp * 100) if vz_imp > 0 else 0
        
        inocente_df = group[group["Papel"] == "Inocente"]
        vt_efetuados = inocente_df["Votos_Efetuados"].sum(); acertos = inocente_df["Acertos_Detetive"].sum()
        taxa_acerto = (acertos / vt_efetuados * 100) if vt_efetuados > 0 else 0
        bode = inocente_df["Votos_Recebidos"].sum()
        
        analytics_list.append({"Jogador": nome, "Taxa de Fuga (%)": round(taxa_fuga, 2), "Faro de Detetive (%)": round(taxa_acerto, 2), "Votos Sofridos Sendo Inocente": int(bode), "Título": "Membro Comum"})
        
    df_resumo = pd.DataFrame(resumo_list); df_analytics = pd.DataFrame(analytics_list)
    
    if not df_analytics.empty:
        mf = df_analytics["Taxa de Fuga (%)"].max(); md = df_analytics["Faro de Detetive (%)"].max(); mb = df_analytics["Votos Sofridos Sendo Inocente"].max()
        for idx, row in df_analytics.iterrows():
            titulos = []
            if row["Taxa de Fuga (%)"] == mf and mf > 0: titulos.append("Loki")
            if row["Faro de Detetive (%)"] == md and md > 0: titulos.append("Sherlock")
            if row["Votos Sofridos Sendo Inocente"] == mb and mb > 0: titulos.append("Bode Expiatório")
            if titulos: df_analytics.at[idx, "Título"] = " | ".join(titulos)

    df_historico_impostores = pd.DataFrame(list(historico_impostores.items()), columns=["Jogador", "Vezes_Impostor"])
    df_palavras = pd.DataFrame(list(palavras_usadas), columns=["Palavra"])
    df_estado_eventos = pd.DataFrame([{"Acumulado_Caos": acumulado_caos, "Acumulado_Trapaca": acumulado_trapaca}])
    
    with engine.begin() as conn:
        df_final_rodadas.to_sql("Rodadas", conn, if_exists="replace", index=False)
        df_resumo.to_sql("Resumo", conn, if_exists="replace", index=False)
        df_analytics.to_sql("Analytics", conn, if_exists="replace", index=False)
        df_historico_impostores.to_sql("Historico_Impostores", conn, if_exists="replace", index=False)
        df_palavras.to_sql("Palavras_Usadas", conn, if_exists="replace", index=False)
        df_estado_eventos.to_sql("Estado_Eventos", conn, if_exists="replace", index=False)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'leoposter_chave_super_secreta'
socketio = SocketIO(app, cors_allowed_origins="*")

salas_ativas = {}

def gerar_codigo_sala():
    return ''.join(random.choices(string.ascii_uppercase, k=4))

# HEARTBEAT DE SINCRONISMO (RAM)
def heartbeat_sala(codigo):
    while codigo in salas_ativas:
        socketio.sleep(1.5)
        sala = salas_ativas.get(codigo)
        if not sala: break
        
        estado_reduzido = {
            'fase': sala['fase_atual'],
            'iteracao': sala['iteracao_fase']
        }
        socketio.emit('sync_estado', estado_reduzido, to=codigo)
        if sala.get('host_sid'):
            socketio.emit('sync_estado_host', estado_reduzido, to=sala['host_sid'])

@socketio.on('pedir_sync_jogador')
def pedir_sync_jogador(dados):
    codigo = dados.get('codigo')
    nome = dados.get('nome')
    sala = salas_ativas.get(codigo)
    if not sala: return
    jogador = next((j for j in sala['jogadores'] if j['nome'] == nome), None)
    if jogador:
        enviar_estado_jogador(sala, jogador)

@socketio.on('pedir_sync_host')
def pedir_sync_host(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return
    verificar_sala_existente({'nome_host': dados.get('nome_host')})

def enviar_estado_jogador(sala, jogador):
    fase = sala.get('fase_atual')
    sid = jogador['sid']
    
    if fase == 'revelacao':
        eh_impostor = jogador['nome'] in sala.get('impostores_atuais', [])
        payload = {
            'fase': fase, 'iteracao': sala['iteracao_fase'],
            'papel': 'impostor' if eh_impostor else 'inocente',
            'tema': sala.get('tema_atual', 'Geral'),
            'caos_ativo': sala.get('caos_ativo_atual', False) if eh_impostor else False,
            'ja_confirmou': jogador['nome'] in sala.get('jogadores_prontos', set()),
            'ja_votou_ja_foi': jogador['nome'] in sala.get('jogadores_ja_foi', set())
        }
        if eh_impostor:
            payload['palavra_ou_dica'] = sala.get('dica_vaga', '')
            if sala.get('trapaca_ativo_atual', False): payload['equipe'] = [i for i in sala.get('impostores_atuais', []) if i != jogador['nome']]
        else: payload['palavra_ou_dica'] = sala.get('palavra_atual', '')
        
        if len(sala['jogadores_prontos']) >= len(sala['jogadores']) and sala.get('modo_jogo') == 'host_jogador':
            socketio.emit('todos_leram_papeis_global', {'fase': fase, 'iteracao': sala['iteracao_fase'], 'tempo': sala['tempo_discussao'], 'primeiro': sala.get('primeiro_falar')}, to=sid)
        else:
            socketio.emit('distribuir_papeis', payload, to=sid)
            
    elif fase == 'votacao':
        jogadores_info = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        ja_votou = jogador['nome'] in sala.get('votos', {})
        socketio.emit('ir_para_tela_votacao', {'fase': fase, 'iteracao': sala['iteracao_fase'], 'jogadores': jogadores_info, 'ja_votou': ja_votou, 'qtd_votos': len(sala['impostores_atuais']), 'caos_geral': sala.get('caos_ativo_atual', False)}, to=sid)
    
    elif fase == 'resultado':
        if sala.get('modo_jogo') == 'host_jogador' and 'ultimo_resultado' in sala:
            res = sala['ultimo_resultado']
            res['fase'] = fase; res['iteracao'] = sala['iteracao_fase']
            socketio.emit('resultado_votacao_global', res, to=sid)
        else:
            socketio.emit('votacao_encerrada_celular', {'fase': fase, 'iteracao': sala['iteracao_fase']}, to=sid)
            
    elif fase == 'lobby': 
        socketio.emit('retorno_lobby_celular', {'fase': fase, 'iteracao': sala['iteracao_fase']}, to=sid)

def checar_todos_leram(codigo):
    sala = salas_ativas.get(codigo)
    if not sala or sala.get('debate_iniciado', False): return
    
    total_prontos = len(sala['jogadores_prontos'])
    total_jogadores = len(sala['jogadores'])
    emit('progresso_leitura_host', {'prontos': total_prontos, 'total': total_jogadores}, to=sala['host_sid'])
    
    if total_jogadores > 0 and total_prontos >= total_jogadores:
        sala['debate_iniciado'] = True 
        emit('todos_leram_papeis', {'tempo': sala['tempo_discussao'], 'iteracao': sala['iteracao_fase']}, to=sala['host_sid'])
        if sala.get('modo_jogo') == 'host_jogador':
            emit('todos_leram_papeis_global', {'tempo': sala['tempo_discussao'], 'primeiro': sala.get('primeiro_falar'), 'iteracao': sala['iteracao_fase']}, to=codigo)

def checar_avanco_ja_foi(codigo):
    sala = salas_ativas.get(codigo)
    if not sala: return
    if len(sala['jogadores']) > 0 and len(sala['jogadores_ja_foi']) >= len(sala['jogadores']):
        sala['jogadores_ja_foi'] = set(); sala['jogadores_prontos'] = set(); sala['confirmacoes_status'] = set()
        sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
        sala['debate_iniciado'] = False

        categoria = sala['tema_atual']
        opcoes = [p for p in BANCO_PALAVRAS[categoria] if p[0] not in sala['palavras_usadas']]
        if not opcoes:
            opcoes = BANCO_PALAVRAS[categoria]
            sala['palavras_usadas'].difference_update({p[0] for p in BANCO_PALAVRAS[categoria]})

        nova_palavra, nova_dica = random.choice(opcoes)
        sala['palavras_usadas'].add(nova_palavra); sala['palavra_atual'] = nova_palavra; sala['dica_vaga'] = nova_dica

        for jogador in sala['jogadores']: enviar_estado_jogador(sala, jogador)
        socketio.start_background_task(monitorar_confirmacoes, codigo, 'revelacao', sala['iteracao_fase'])
        emit('progresso_leitura_host', {'prontos': 0, 'total': len(sala['jogadores'])}, to=sala['host_sid'])
        emit('travar_timer_host', {}, to=sala['host_sid'])

@socketio.on('confirmar_status')
def confirmar_status(dados):
    codigo = dados.get('codigo'); nome = dados.get('nome'); status = dados.get('status')
    sala = salas_ativas.get(codigo)
    if sala and sala.get('fase_atual') == status:
        if 'confirmacoes_status' not in sala: sala['confirmacoes_status'] = set()
        sala['confirmacoes_status'].add(nome)

@app.route('/')
def tela_jogador(): return render_template('jogador.html')

@app.route('/host')
def tela_admin(): return render_template('admin.html')

@app.route('/testador')
def tela_testador(): return render_template('testador.html')

@socketio.on('verificar_sala_existente')
def verificar_sala_existente(dados):
    if salas_ativas:
        codigo = list(salas_ativas.keys())[0] 
        sala = salas_ativas[codigo]
        sala['host_sid'] = request.sid
        join_room(codigo)
        
        nome_host = dados.get('nome_host') if dados else None
        if nome_host:
            for j in sala['jogadores']:
                if j['nome'] == nome_host:
                    j['sid'] = request.sid
                    break
        
        estado = {
            'codigo': codigo, 'fase_atual': sala['fase_atual'], 'iteracao': sala['iteracao_fase'],
            'jogadores': [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']],
            'temas': list(BANCO_PALAVRAS.keys()), 'modo_jogo': sala.get('modo_jogo', 'host'),
            'primeiro_falar': sala.get('primeiro_falar', ''), 'votos_computados': len(sala.get('votos', {})),
            'total_jogadores': len(sala['jogadores']), 'ultimo_resultado': sala.get('ultimo_resultado', {})
        }
        emit('sala_recuperada', estado)
    else: emit('nenhuma_sala_ativa')

@socketio.on('criar_sala')
def criar_sala():
    salas_ativas.clear() 
    codigo = gerar_codigo_sala()
    salas_ativas[codigo] = {
        'host_sid': request.sid, 'id_partida': str(uuid.uuid4())[:8], 'jogadores': [],
        'historico_impostores': {}, 'palavras_usadas': set(), 'acumulado_caos': 10,
        'acumulado_trapaca': 20, 'impostores_ultima_rodada': [], 'placar': {},
        'fila_interrogatorio': [], 'fase_atual': 'lobby', 'iteracao_fase': 0,
        'jogadores_prontos': set(), 'jogadores_ja_foi': set(),
        'impostores_atuais': [], 'palavra_atual': '', 'tempo_discussao': 120, 'votos': {},
        'modo_jogo': 'host', 'debate_iniciado': False, 'ultimo_resultado': {}
    }
    carregar_dados_sala_do_banco(salas_ativas[codigo])
    join_room(codigo)
    socketio.start_background_task(heartbeat_sala, codigo)
    emit('sala_criada_sucesso', {'codigo': codigo, 'temas': list(BANCO_PALAVRAS.keys())})

@socketio.on('destruir_sala')
def destruir_sala(dados):
    if salas_ativas:
        codigo = list(salas_ativas.keys())[0]
        emit('sala_destruida', {}, to=codigo) 
        salas_ativas.clear()

@socketio.on('forcar_avanco')
def forcar_avanco(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return
    
    if sala['fase_atual'] == 'revelacao':
        sala['jogadores_prontos'] = set([j['nome'] for j in sala['jogadores']])
        checar_todos_leram(codigo)
    elif sala['fase_atual'] == 'votacao':
        encerrar_votacao_interna(codigo)

@socketio.on('solicitar_coroacao')
def solicitar_coroacao(dados):
    emit('receber_coroacao', {'tipo': dados.get('tipo'), 'ranking': obter_ranking_dinamico(dados.get('tipo'))}, to=request.sid)

@socketio.on('deletar_jogador_banco')
def deletar_jogador_banco(dados):
    nome = dados.get('nome')
    if not nome: return
    try:
        with engine.begin() as conn:
            conn.execute(text('DELETE FROM "Rodadas" WHERE "Nome" = :n'), {"n": nome})
            conn.execute(text('DELETE FROM "Resumo" WHERE "Jogador" = :n'), {"n": nome})
            conn.execute(text('DELETE FROM "Analytics" WHERE "Jogador" = :n'), {"n": nome})
            conn.execute(text('DELETE FROM "Historico_Impostores" WHERE "Jogador" = :n'), {"n": nome})
            for sala in salas_ativas.values():
                if nome in sala['historico_impostores']: del sala['historico_impostores'][nome]
                if nome in sala['placar']: del sala['placar'][nome]
        emit('alerta_host', {'mensagem': f'Todos os dados de {nome} foram apagados com sucesso!'}, to=request.sid)
    except Exception as e:
        emit('alerta_host', {'mensagem': f'Erro ao deletar jogador: {str(e)}'}, to=request.sid)

@socketio.on('resetar_banco')
def resetar_banco(dados):
    tipo = dados.get('tipo')
    try:
        with engine.begin() as conn:
            if tipo in ['palavras', 'tudo']:
                try: conn.execute(text('DELETE FROM "Palavras_Usadas"'))
                except Exception: pass
                for sala in salas_ativas.values(): sala['palavras_usadas'] = set()
            if tipo in ['partidas', 'tudo']:
                for t in ["Rodadas", "Resumo", "Analytics", "Historico_Impostores", "Estado_Eventos"]:
                    try: conn.execute(text(f'DELETE FROM "{t}"'))
                    except Exception: pass
                for sala in salas_ativas.values():
                    sala['historico_impostores'] = {}
                    sala['acumulado_caos'] = 10; sala['acumulado_trapaca'] = 20
                    sala['placar'] = {}; sala['impostores_ultima_rodada'] = []
        emit('alerta_host', {'mensagem': 'Operação concluída com sucesso! Banco atualizado.'}, to=request.sid)
    except Exception as e:
        emit('alerta_host', {'mensagem': f'Erro ao processar o reset: {str(e)}'}, to=request.sid)

@socketio.on('entrar_na_sala')
def entrar_na_sala(dados):
    nome = dados.get('nome', '').strip()
    codigo = dados.get('codigo', '').strip().upper()

    if codigo not in salas_ativas:
        emit('erro_conexao', {'mensagem': 'Sala não encontrada! A partida foi encerrada ou o código está incorreto.'}, to=request.sid); return

    sala = salas_ativas[codigo]
    jogador_existente = next((j for j in sala['jogadores'] if j['nome'].lower() == nome.lower()), None)
    
    if jogador_existente:
        jogador_existente['sid'] = request.sid
        join_room(codigo)
        emit('entrada_sucesso', {'nome': jogador_existente['nome'], 'reconexao': True}, to=request.sid)
        enviar_estado_jogador(sala, jogador_existente)
        lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
        return

    if sala['fase_atual'] != 'lobby':
        emit('erro_conexao', {'mensagem': 'A partida já começou! Você não pode entrar no meio do jogo.'}, to=request.sid); return

    if nome not in sala['historico_impostores']: sala['historico_impostores'][nome] = 0
    emojis_usados = [j['emoji'] for j in sala['jogadores']]
    opcoes = [e for e in EMOJIS_DISPONIVEIS if e not in emojis_usados]
    meu_emoji = random.choice(opcoes if opcoes else EMOJIS_DISPONIVEIS)

    join_room(codigo)
    sala['jogadores'].append({'sid': request.sid, 'nome': nome, 'emoji': meu_emoji})
    lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
    emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
    emit('entrada_sucesso', {'nome': nome, 'reconexao': False}, to=request.sid)
    emit('retorno_lobby_celular', {'fase': 'lobby', 'iteracao': sala['iteracao_fase']}, to=request.sid)

@socketio.on('sair_da_sala')
def sair_da_sala(dados):
    codigo = dados.get('codigo'); nome_sair = dados.get('nome')
    sala = salas_ativas.get(codigo)
    if sala:
        jogador = next((j for j in sala['jogadores'] if j['nome'] == nome_sair), None)
        if jogador:
            sala['jogadores'].remove(jogador)
            lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
            emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
            
            if sala['fase_atual'] == 'revelacao':
                sala['jogadores_prontos'].discard(nome_sair); sala['jogadores_ja_foi'].discard(nome_sair)
                checar_avanco_ja_foi(codigo)
                checar_todos_leram(codigo)
            elif sala['fase_atual'] == 'votacao':
                if nome_sair in sala['votos']: del sala['votos'][nome_sair]
                emit('voto_recebido_host', {'total_votos': len(sala['votos']), 'total_jogadores': len(sala['jogadores'])}, to=sala['host_sid'])
                if len(sala['jogadores']) > 0 and len(sala['votos']) >= len(sala['jogadores']):
                    encerrar_votacao_interna(codigo)

@socketio.on('trocar_emoji')
def trocar_emoji(dados):
    codigo = dados.get('codigo'); nome = dados.get('nome'); novo_emoji = dados.get('emoji')
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'lobby': return

    emojis_em_uso = [j['emoji'] for j in sala['jogadores']]
    if novo_emoji not in emojis_em_uso and novo_emoji in EMOJIS_DISPONIVEIS:
        jogador = next((j for j in sala['jogadores'] if j['nome'] == nome), None)
        if jogador:
            jogador['emoji'] = novo_emoji
            lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
            emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)

@socketio.on('expulsar_jogador')
def expulsar_jogador(dados):
    codigo = dados.get('codigo'); nome_expulso = dados.get('nome')
    sala = salas_ativas.get(codigo)
    if sala:
        jogador = next((j for j in sala['jogadores'] if j['nome'] == nome_expulso), None)
        if jogador:
            sala['jogadores'].remove(jogador)
            emit('foi_expulso', {}, to=jogador['sid'])
            lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
            emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
            
            if sala['fase_atual'] == 'revelacao':
                sala['jogadores_prontos'].discard(nome_expulso); sala['jogadores_ja_foi'].discard(nome_expulso)
                checar_avanco_ja_foi(codigo)
                checar_todos_leram(codigo)
            elif sala['fase_atual'] == 'votacao':
                if nome_expulso in sala['votos']: del sala['votos'][nome_expulso]
                emit('voto_recebido_host', {'total_votos': len(sala['votos']), 'total_jogadores': len(sala['jogadores'])}, to=sala['host_sid'])
                if len(sala['jogadores']) > 0 and len(sala['votos']) >= len(sala['jogadores']):
                    encerrar_votacao_interna(codigo)

@socketio.on('voltar_para_lobby')
def voltar_para_lobby(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if sala:
        sala['fase_atual'] = 'lobby'; sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
        sala['votos'] = {}
        sala['jogadores_prontos'] = set()
        sala['jogadores_ja_foi'] = set()
        sala['debate_iniciado'] = False
        
        emit('retorno_lobby_host', {'fase': 'lobby', 'iteracao': sala['iteracao_fase']}, to=sala['host_sid'])
        lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)

@socketio.on('iniciar_partida')
def iniciar_partida(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala or len(sala['jogadores']) < 3: return

    sala['modo_jogo'] = dados.get('modo_jogo', 'host')
    sala['fase_atual'] = 'revelacao'
    sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    sala['jogadores_prontos'] = set(); sala['jogadores_ja_foi'] = set(); sala['votos'] = {}
    sala['tempo_discussao'] = int(dados.get('tempo', 120))
    sala['debate_iniciado'] = False 

    categoria = dados.get('categoria')
    if categoria == "Aleatório": categoria = random.choice(list(BANCO_PALAVRAS.keys()))
    
    opcoes = [p for p in BANCO_PALAVRAS[categoria] if p[0] not in sala['palavras_usadas']]
    if not opcoes:
        opcoes = BANCO_PALAVRAS[categoria]
        sala['palavras_usadas'].difference_update({p[0] for p in BANCO_PALAVRAS[categoria]})

    palavra_secreta, dica_vaga = random.choice(opcoes)
    sala['palavras_usadas'].add(palavra_secreta)

    prob_caos = int(dados.get('caos', 10)); prob_trapaca = int(dados.get('trapaca', 20))
    caos_ativo = False
    
    if len(sala['jogadores']) >= 3:
        caos_ativo = random.random() < (sala['acumulado_caos'] / 100.0)
        if caos_ativo: sala['acumulado_caos'] = prob_caos
        else: sala['acumulado_caos'] = min(100, sala['acumulado_caos'] + prob_caos)

    qtd_impostores = min(len(sala['jogadores']) - 1, int(dados.get('imp_base', 1)) + (1 if caos_ativo else 0))

    trapaca_ativo = False
    if qtd_impostores > 1:
        trapaca_ativo = random.random() < (sala['acumulado_trapaca'] / 100.0)
        if trapaca_ativo: sala['acumulado_trapaca'] = prob_trapaca
        else: sala['acumulado_trapaca'] = min(100, sala['acumulado_trapaca'] + prob_trapaca)
    
    candidatos = [j['nome'] for j in sala['jogadores']]
    pesos = [max(0.01, 1.0 - (0.33 * min(sala['historico_impostores'].get(j, 0), 3))) * (0.5 if j in sala['impostores_ultima_rodada'] else 1.0) for j in candidatos]
    
    impostores_sorteados = []
    for _ in range(qtd_impostores):
        escolhido = random.choices(candidatos, weights=pesos, k=1)[0]
        impostores_sorteados.append(escolhido)
        idx = candidatos.index(escolhido)
        candidatos.pop(idx); pesos.pop(idx)

    sala['impostores_ultima_rodada'] = list(impostores_sorteados)
    for imp in impostores_sorteados: sala['historico_impostores'][imp] += 1
    if sum(1 for c in sala['historico_impostores'].values() if c >= 3) >= 3:
        for k in sala['historico_impostores']: sala['historico_impostores'][k] = 0

    sala['impostores_atuais'] = impostores_sorteados; sala['palavra_atual'] = palavra_secreta
    sala['tema_atual'] = categoria; sala['dica_vaga'] = dica_vaga
    sala['caos_ativo_atual'] = caos_ativo; sala['trapaca_ativo_atual'] = trapaca_ativo

    primeiro = random.choice(sala['jogadores'])
    sala['primeiro_falar'] = f"{primeiro['emoji']} {primeiro['nome']}"

    for jogador in sala['jogadores']: enviar_estado_jogador(sala, jogador)
    emit('partida_iniciada_host', {'primeiro': sala['primeiro_falar'], 'total_jogadores': len(sala['jogadores']), 'iteracao': sala['iteracao_fase']}, to=sala['host_sid'])

@socketio.on('clicou_ja_foi')
def clicou_ja_foi(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'revelacao': return

    sala['jogadores_ja_foi'].add(dados.get('nome'))
    checar_avanco_ja_foi(codigo)

@socketio.on('confirmar_leitura_papel')
def confirmar_leitura_papel(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return

    sala['jogadores_prontos'].add(dados.get('nome'))
    checar_todos_leram(codigo)

@socketio.on('iniciar_votacao')
def iniciar_votacao(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return
    
    sala['fase_atual'] = 'votacao'; sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    sala['votos'] = {}
    
    for jogador in sala['jogadores']: enviar_estado_jogador(sala, jogador)
    emit('votacao_iniciada_host', {'total_jogadores': len(sala['jogadores']), 'iteracao': sala['iteracao_fase']}, to=sala['host_sid'])

@socketio.on('enviar_voto')
def receber_voto(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return
    
    sala['votos'][dados.get('nome')] = dados.get('voto') 
    emit('voto_recebido_host', {'total_votos': len(sala['votos']), 'total_jogadores': len(sala['jogadores'])}, to=sala['host_sid'])
    if len(sala['votos']) >= len(sala['jogadores']): encerrar_votacao_interna(codigo)

def encerrar_votacao_interna(codigo):
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] == 'resultado': return

    sala['fase_atual'] = 'resultado'; sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    
    votos = sala.get('votos', {})
    total_inocentes_mesa = sum(1 for j in sala['jogadores'] if j['nome'] not in sala['impostores_atuais'])
    limite_para_pegar = max(1, total_inocentes_mesa / 2.0)
    
    contagem = {}
    for eleitor, lista_votados in votos.items():
        if eleitor not in sala['impostores_atuais']:
            for votado in lista_votados: contagem[votado] = contagem.get(votado, 0) + 1
            
    eliminados_de_fato = [k for k, v in contagem.items() if v >= limite_para_pegar]

    pontos_da_rodada = {}; detalhes_rodada = {}
    for j in sala['jogadores']:
        nome = j['nome']; eh_impostor = nome in sala['impostores_atuais']
        votos_recebidos = contagem.get(nome, 0); votos_dados = votos.get(nome, [])
        acertos_detetive = sum(1 for v in votos_dados if v in sala['impostores_atuais']) if not eh_impostor else 0
        
        pts = 0
        if eh_impostor:
            if nome not in eliminados_de_fato: pts = 2 
        else:
            if acertos_detetive > 0: pts = acertos_detetive
            
        pontos_da_rodada[nome] = pts
        sala['placar'][nome] = sala['placar'].get(nome, 0) + pts
        detalhes_rodada[nome] = {"Papel": "Impostor" if eh_impostor else "Inocente", "Votos_Recebidos": votos_recebidos, "Acertos_Detetive": acertos_detetive, "Votos_Efetuados": len(votos_dados)}
        
    try: salvar_banco_dados(pontos_da_rodada, sala['placar'], sala['id_partida'], detalhes_rodada, sala['historico_impostores'], sala['palavras_usadas'], sala['acumulado_caos'], sala['acumulado_trapaca'])
    except Exception as e: print("Erro ao salvar no banco:", e)

    destaques_resultado = []
    for imp in sala['impostores_atuais']:
        destaques_resultado.append({
            'nome': imp, 'emoji': next((j['emoji'] for j in sala['jogadores'] if j['nome'] == imp), "👤"),
            'papel': 'Impostor', 'status': 'Foi pego!' if imp in eliminados_de_fato else 'Conseguiu fugir!'
        })
    
    for elim in eliminados_de_fato:
        if elim not in sala['impostores_atuais']:
            destaques_resultado.append({
                'nome': elim, 'emoji': next((j['emoji'] for j in sala['jogadores'] if j['nome'] == elim), "👤"),
                'papel': 'Inocente', 'status': 'Executado por engano!'
            })

    outros_votados = [{'nome': nome, 'emoji': next((j['emoji'] for j in sala['jogadores'] if j['nome'] == nome), "👤"), 'votos': qtd} for nome, qtd in sorted(contagem.items(), key=lambda x: x[1], reverse=True) if nome not in sala['impostores_atuais'] and nome not in eliminados_de_fato and qtd > 0]

    payload_resultado = {
        'destaques': destaques_resultado,
        'outros_votados': outros_votados,
        'palavra_atual': sala['palavra_atual'],
        'pontos': sala['placar'],
        'iteracao': sala['iteracao_fase'],
        'fase': 'resultado'
    }
    
    sala['ultimo_resultado'] = payload_resultado

    for jogador in sala['jogadores']: enviar_estado_jogador(sala, jogador)
    emit('resultado_votacao_host', payload_resultado, to=sala['host_sid'])

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
