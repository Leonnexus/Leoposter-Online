Aqui estão os três ficheiros completos, corrigidos e com todas as novas funcionalidades integradas (bloqueio de nomes repetidos, sliders de probabilidade, ocultação de alertas para inocentes, sistema de expulsão, alocação de emojis e o ecrã de suspense com contagem regressiva responsivo).

1. app.py
Python
# ==========================================
# OBRIGATÓRIO: ESTE DEVE SER O PRIMEIRO COMANDO DO APP.PY
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
from sqlalchemy import create_engine

from banco_palavras import BANCO_PALAVRAS

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///leoposter.db')
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

EMOJIS_DISPONIVEIS = ["🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐨", "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🐙", "🐢", "🦕", "🦞", "🦄", "👽", "🤖", "👻", "👾"]

def obter_ranking_dinamico(tipo):
    try:
        df = pd.read_sql_table("Rodadas", engine)
        df = df[df["Dia"] != "SOMATÓRIA"].copy()
        if df.empty: return []

        df["Data_Real"] = pd.to_datetime(df["Dia"], format="%d/%m/%Y", errors="coerce")
        hoje = pd.Timestamp(datetime.now().date())

        if tipo == "Dia":
            df = df[df["Data_Real"] == hoje]
        elif tipo == "Semana":
            df = df[(df["Data_Real"].dt.isocalendar().week == hoje.isocalendar().week) &
                    (df["Data_Real"].dt.isocalendar().year == hoje.isocalendar().year)]
        
        if df.empty: return []

        ranking = []
        for nome, group in df.groupby("Nome"):
            pts = group["Pontos na Rodada"].sum()
            rodadas = len(group)
            media = round(pts / rodadas, 2) if rodadas > 0 else 0
            ranking.append({"Jogador": nome, "Pontuação Total": int(pts), "Média de Pontos": float(media)})

        ranking.sort(key=lambda x: (x["Média de Pontos"], x["Pontuação Total"]), reverse=True)
        return ranking
    except ValueError:
        return []

def carregar_dados_sala_do_banco(sala):
    try:
        df_palavras = pd.read_sql_table("Palavras_Usadas", engine)
        sala['palavras_usadas'] = set(df_palavras["Palavra"].dropna().astype(str).tolist())
    except ValueError:
        pass

    try:
        df_eventos = pd.read_sql_table("Estado_Eventos", engine)
        if not df_eventos.empty:
            sala['acumulado_caos'] = int(df_eventos["Acumulado_Caos"].iloc[0])
            sala['acumulado_trapaca'] = int(df_eventos["Acumulado_Trapaca"].iloc[0])
    except ValueError:
        pass

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
        partidas = group["ID_Partida"].nunique()
        rodadas = len(group)
        pts_totais = group["Pontos na Rodada"].sum()
        media = pts_totais / rodadas if rodadas > 0 else 0
        vitorias = sum(group["Pontos na Rodada"] > 0)
        
        resumo_list.append({"Jogador": nome, "Partidas Jogadas": int(partidas), "Rodadas Jogadas": int(rodadas), "Pontuação Total": int(pts_totais), "Média de Pontos": round(float(media), 2), "Vitórias em Rodadas": int(vitorias)})
        
        impostor_df = group[group["Papel"] == "Impostor"]
        vz_imp = len(impostor_df)
        fugas = len(impostor_df[impostor_df["Pontos na Rodada"] > 0])
        taxa_fuga = (fugas / vz_imp * 100) if vz_imp > 0 else 0
        
        inocente_df = group[group["Papel"] == "Inocente"]
        vt_efetuados = inocente_df["Votos_Efetuados"].sum()
        acertos = inocente_df["Acertos_Detetive"].sum()
        taxa_acerto = (acertos / vt_efetuados * 100) if vt_efetuados > 0 else 0
        bode = inocente_df["Votos_Recebidos"].sum()
        
        analytics_list.append({"Jogador": nome, "Taxa de Fuga (%)": round(taxa_fuga, 2), "Faro de Detetive (%)": round(taxa_acerto, 2), "Votos Sofridos Sendo Inocente": int(bode), "Título": "Membro Comum"})
        
    df_resumo = pd.DataFrame(resumo_list)
    df_analytics = pd.DataFrame(analytics_list)
    
    if not df_analytics.empty:
        mf, md, mb = df_analytics["Taxa de Fuga (%)"].max(), df_analytics["Faro de Detetive (%)"].max(), df_analytics["Votos Sofridos Sendo Inocente"].max()
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
    while True:
        codigo = ''.join(random.choices(string.ascii_uppercase, k=4))
        if codigo not in salas_ativas:
            return codigo

@app.route('/')
def tela_jogador():
    return render_template('jogador.html')

@app.route('/host')
def tela_admin():
    return render_template('admin.html')

@socketio.on('criar_sala')
def criar_sala():
    codigo = gerar_codigo_sala()
    salas_ativas[codigo] = {
        'host_sid': request.sid,
        'id_partida': str(uuid.uuid4())[:8],
        'jogadores': [],
        'historico_impostores': {},
        'palavras_usadas': set(),
        'acumulado_caos': 10,
        'acumulado_trapaca': 20,
        'impostores_ultima_rodada': [] ,
        'placar': {},
        'fila_interrogatorio': []
    }
    carregar_dados_sala_do_banco(salas_ativas[codigo])
    join_room(codigo)
    temas_disponiveis = list(BANCO_PALAVRAS.keys())
    emit('sala_criada_sucesso', {'codigo': codigo, 'temas': temas_disponiveis})

@socketio.on('solicitar_coroacao')
def solicitar_coroacao(dados):
    tipo = dados.get('tipo')
    ranking = obter_ranking_dinamico(tipo)
    emit('receber_coroacao', {'tipo': tipo, 'ranking': ranking}, to=request.sid)

@socketio.on('entrar_na_sala')
def entrar_na_sala(dados):
    nome = dados.get('nome', '').strip()
    codigo = dados.get('codigo', '').strip().upper()

    if codigo in salas_ativas:
        sala = salas_ativas[codigo]
        
        if any(j['nome'].lower() == nome.lower() for j in sala['jogadores']):
            emit('erro_conexao', {'mensagem': 'Este nome já está em uso na sala. Escolha outro!'})
            return

        if nome not in sala['historico_impostores']:
            sala['historico_impostores'][nome] = 0

        emojis_usados = [j['emoji'] for j in sala['jogadores']]
        opcoes = [e for e in EMOJIS_DISPONIVEIS if e not in emojis_usados]
        if not opcoes: opcoes = EMOJIS_DISPONIVEIS
        meu_emoji = random.choice(opcoes)

        join_room(codigo)
        sala['jogadores'].append({'sid': request.sid, 'nome': nome, 'emoji': meu_emoji})
        
        lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
        emit('entrada_sucesso', {'nome': nome})
    else:
        emit('erro_conexao', {'mensagem': 'Sala não encontrada!'})

@socketio.on('expulsar_jogador')
def expulsar_jogador(dados):
    codigo = dados.get('codigo')
    nome_expulso = dados.get('nome')
    sala = salas_ativas.get(codigo)
    if sala:
        jogador = next((j for j in sala['jogadores'] if j['nome'] == nome_expulso), None)
        if jogador:
            sala['jogadores'].remove(jogador)
            emit('foi_expulso', {}, to=jogador['sid'])
            lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
            emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)

@socketio.on('iniciar_partida')
def iniciar_partida(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return

    categoria = dados.get('categoria')
    imp_base = int(dados.get('imp_base', 1))
    prob_caos = int(dados.get('caos', 10))
    prob_trapaca = int(dados.get('trapaca', 20))

    sala['acumulado_caos'] = max(sala['acumulado_caos'], prob_caos)
    sala['acumulado_trapaca'] = max(sala['acumulado_trapaca'], prob_trapaca)

    if categoria == "Aleatório":
        categoria = random.choice(list(BANCO_PALAVRAS.keys()))
    
    opcoes = [p for p in BANCO_PALAVRAS[categoria] if p[0] not in sala['palavras_usadas']]
    if not opcoes:
        opcoes = BANCO_PALAVRAS[categoria]
        sala['palavras_usadas'].difference_update({p[0] for p in BANCO_PALAVRAS[categoria]})

    palavra_secreta, dica_vaga = random.choice(opcoes)
    sala['palavras_usadas'].add(palavra_secreta)

    caos_ativo = random.random() < (sala['acumulado_caos'] / 100.0)
    if caos_ativo: sala['acumulado_caos'] = prob_caos
    else: sala['acumulado_caos'] = min(100, sala['acumulado_caos'] + prob_caos)

    qtd_impostores = min(len(sala['jogadores']) - 1, imp_base + (1 if caos_ativo else 0))

    trapaca_ativo = False
    if qtd_impostores > 1:
        trapaca_ativo = random.random() < (sala['acumulado_trapaca'] / 100.0)
        if trapaca_ativo: sala['acumulado_trapaca'] = prob_trapaca
        else: sala['acumulado_trapaca'] = min(100, sala['acumulado_trapaca'] + prob_trapaca)
    
    candidatos = [j['nome'] for j in sala['jogadores']]
    pesos = []
    for j in candidatos:
        peso_base = max(0.01, 1.0 - (0.33 * min(sala['historico_impostores'].get(j, 0), 3)))
        if j in sala['impostores_ultima_rodada']:
            peso_base *= 0.5
        pesos.append(peso_base)
    
    impostores_sorteados = []
    for _ in range(qtd_impostores):
        escolhido = random.choices(candidatos, weights=pesos, k=1)[0]
        impostores_sorteados.append(escolhido)
        idx = candidatos.index(escolhido)
        candidatos.pop(idx); pesos.pop(idx)

    sala['impostores_ultima_rodada'] = list(impostores_sorteados)

    for imp in impostores_sorteados:
        sala['historico_impostores'][imp] += 1
    if sum(1 for c in sala['historico_impostores'].values() if c >= 3) >= 3:
        for k in sala['historico_impostores']: sala['historico_impostores'][k] = 0

    sala['impostores_atuais'] = impostores_sorteados
    sala['palavra_atual'] = palavra_secreta
    sala['votos'] = {}

    sala['fila_interrogatorio'] = list(sala['jogadores'])
    random.shuffle(sala['fila_interrogatorio'])
    primeiro = sala['fila_interrogatorio'].pop(0) if sala['fila_interrogatorio'] else None
    primeiro_nome_fmt = f"{primeiro['emoji']} {primeiro['nome']}" if primeiro else "Ninguém"

    for jogador in sala['jogadores']:
        eh_impostor = jogador['nome'] in impostores_sorteados
        payload = {
            'papel': 'impostor' if eh_impostor else 'inocente',
            'tema': categoria,
            'caos_ativo': caos_ativo if eh_impostor else False 
        }
        if eh_impostor:
            payload['palavra_ou_dica'] = dica_vaga
            if trapaca_ativo:
                payload['equipe'] = [i for i in impostores_sorteados if i != jogador['nome']]
        else:
            payload['palavra_ou_dica'] = palavra_secreta

        emit('distribuir_papeis', payload, to=jogador['sid'])

    emit('partida_iniciada_host', {'primeiro': primeiro_nome_fmt}, to=sala['host_sid'])

@socketio.on('iniciar_votacao')
def iniciar_votacao(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return
    jogadores_info = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
    emit('ir_para_tela_votacao', {'jogadores': jogadores_info}, to=codigo)
    emit('votacao_iniciada_host', {'total_jogadores': len(sala['jogadores'])}, to=sala['host_sid'])

@socketio.on('enviar_voto')
def receber_voto(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if sala:
        sala['votos'][dados.get('nome')] = dados.get('voto')
        emit('voto_recebido_host', {'total_votos': len(sala['votos']), 'total_jogadores': len(sala['jogadores'])}, to=sala['host_sid'])

@socketio.on('encerrar_votacao')
def encerrar_votacao(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return

    votos = sala.get('votos', {})
    contagem = {}
    for eleitor, votado in votos.items():
        contagem[votado] = contagem.get(votado, 0) + 1
    
    eliminados = []
    if contagem:
        max_votos = max(contagem.values())
        eliminados = [k for k, v in contagem.items() if v == max_votos]

    foi_eliminado_de_fato = eliminados[0] if len(eliminados) == 1 else None

    pontos_da_rodada = {}
    detalhes_rodada = {}
    
    for j in sala['jogadores']:
        nome = j['nome']
        eh_impostor = nome in sala['impostores_atuais']
        votos_recebidos = contagem.get(nome, 0)
        voto_dado = votos.get(nome, "")
        acertos_detetive = 1 if (not eh_impostor and voto_dado in sala['impostores_atuais']) else 0
        
        pts = 0
        if eh_impostor:
            if nome != foi_eliminado_de_fato: pts = 2
        else:
            if acertos_detetive > 0: pts = 1
            
        pontos_da_rodada[nome] = pts
        sala['placar'][nome] = sala['placar'].get(nome, 0) + pts
        
        detalhes_rodada[nome] = {
            "Papel": "Impostor" if eh_impostor else "Inocente",
            "Votos_Recebidos": votos_recebidos,
            "Acertos_Detetive": acertos_detetive,
            "Votos_Efetuados": 1 if voto_dado else 0
        }
        
    try:
        salvar_banco_dados(pontos_da_rodada, sala['placar'], sala['id_partida'], detalhes_rodada, sala['historico_impostores'], sala['palavras_usadas'], sala['acumulado_caos'], sala['acumulado_trapaca'])
    except Exception as e:
        print("Erro ao salvar no banco:", e)

    eliminados_info = []
    for e in eliminados:
        emoji_e = next((j['emoji'] for j in sala['jogadores'] if j['nome'] == e), "👤")
        eliminados_info.append({
            'nome': e,
            'emoji': emoji_e,
            'eh_impostor': e in sala['impostores_atuais']
        })

    emit('resultado_votacao_host', {
        'eliminados': eliminados_info,
        'palavra_atual': sala['palavra_atual'],
        'pontos': sala['placar']
    }, to=sala['host_sid'])
    
    emit('votacao_encerrada_celular', {}, to=codigo)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
