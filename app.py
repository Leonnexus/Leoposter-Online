# ==========================================
import eventlet
eventlet.monkey_patch()
# ==========================================

import os
import random
import secrets
import string
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, emit
import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from banco_palavras import BANCO_PALAVRAS

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///leoposter.db')
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '5813')
TEMPO_EXPIRACAO_SALA = 6 * 3600  # 6 horas de inatividade

engine = create_engine(DATABASE_URL, poolclass=NullPool)
db_lock = threading.Lock()

EMOJIS_DISPONIVEIS = [
    "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼", "🐨", "🐯",
    "🦁", "🐮", "🐷", "🐸", "🐵", "🐙", "🐢", "🦕", "🦞", "🦄",
    "👽", "🤖", "👻", "👾", "🦉", "🦇", "🐺", "🐗", "🐴", "🐝",
    "🐛", "🦋", "🐌", "🐞", "🐜", "🐠", "🐬", "🐳", "🦈", "🐊",
    "🐅", "🐆", "🦓", "🦍", "🐘", "🦛", "🦏", "🐪", "🦒", "🦘"
]


def chave_palavra(categoria, palavra):
    return f"{categoria}||{palavra}"


def encontrar_jogador(sala, nome):
    if not sala or not nome:
        return None
    alvo = nome.strip().lower()
    return next((j for j in sala['jogadores'] if j['nome'].lower() == alvo), None)


def autorizado_admin(dados):
    if not isinstance(dados, dict):
        return False
    senha = str(dados.get('senha') or '')
    return secrets.compare_digest(senha, str(ADMIN_PASSWORD))


def autorizado_host(sala, dados):
    if not sala or not isinstance(dados, dict):
        return False
    if autorizado_admin(dados):
        return True
    token = str(dados.get('host_token') or '')
    token_sala = str(sala.get('host_token') or '')
    return bool(token and token_sala and secrets.compare_digest(token, token_sala))


def registrar_atividade(sala):
    if sala:
        sala['ultima_atividade'] = time.time()


def obter_ranking_dinamico(tipo):
    try:
        with db_lock:
            if not inspect(engine).has_table("Rodadas"):
                return []
            df = pd.read_sql_table("Rodadas", engine)
        df = df[df["Dia"] != "SOMATÓRIA"].copy()
        if df.empty:
            return []

        df["Data_Real"] = pd.to_datetime(df["Dia"], format="%d/%m/%Y", errors="coerce")
        hoje = pd.Timestamp(datetime.now().date())

        if tipo == "Dia":
            df = df[df["Data_Real"] == hoje]
        elif tipo == "Semana":
            df = df[
                (df["Data_Real"].dt.isocalendar().week == hoje.isocalendar().week)
                & (df["Data_Real"].dt.isocalendar().year == hoje.isocalendar().year)
            ]

        if df.empty:
            return []

        ranking = []
        for nome, group in df.groupby("Nome"):
            pts = group["Pontos na Rodada"].sum()
            rodadas = len(group)
            media = round(pts / rodadas, 2) if rodadas > 0 else 0
            ranking.append({
                "Jogador": nome,
                "Pontuação Total": int(pts),
                "Média de Pontos": float(media)
            })

        ranking.sort(key=lambda x: (x["Média de Pontos"], x["Pontuação Total"]), reverse=True)
        return ranking
    except Exception as e:
        print("Erro ao obter ranking:", e)
        return []


def carregar_dados_sala_do_banco(sala):
    try:
        with db_lock:
            insp = inspect(engine)
            if insp.has_table("Palavras_Usadas"):
                df_palavras = pd.read_sql_table("Palavras_Usadas", engine)
                if "Palavra" in df_palavras.columns:
                    sala['palavras_usadas'] = set(df_palavras["Palavra"].dropna().astype(str).tolist())
            if insp.has_table("Estado_Eventos"):
                df_eventos = pd.read_sql_table("Estado_Eventos", engine)
                if not df_eventos.empty:
                    sala['acumulado_caos'] = int(df_eventos["Acumulado_Caos"].iloc[0])
                    sala['acumulado_trapaca'] = int(df_eventos["Acumulado_Trapaca"].iloc[0])
    except Exception as e:
        print("Aviso ao carregar dados iniciais do banco:", e)


def salvar_banco_dados(pontos_da_rodada, placar, id_partida, detalhes_rodada, palavras_usadas, acumulado_caos, acumulado_trapaca):
    dia_hoje = datetime.now().strftime("%d/%m/%Y")
    novas_linhas = [{
        "ID_Partida": id_partida,
        "Nome": j,
        "Papel": detalhes_rodada[j]["Papel"],
        "Votos_Recebidos": detalhes_rodada[j]["Votos_Recebidos"],
        "Acertos_Detetive": detalhes_rodada[j]["Acertos_Detetive"],
        "Votos_Efetuados": detalhes_rodada[j]["Votos_Efetuados"],
        "Pontos na Rodada": int(pts),
        "Placar Geral da Partida": int(placar[j]),
        "Dia": str(dia_hoje)
    } for j, pts in pontos_da_rodada.items()]

    try:
        with db_lock:
            with engine.begin() as conn:
                insp = inspect(conn)

                if insp.has_table("Rodadas"):
                    df = pd.read_sql_table("Rodadas", conn)
                    df = df[df["Dia"] != "SOMATÓRIA"].copy()
                else:
                    df = pd.DataFrame(columns=[
                        "ID_Partida", "Nome", "Papel", "Votos_Recebidos",
                        "Acertos_Detetive", "Votos_Efetuados", "Pontos na Rodada",
                        "Placar Geral da Partida", "Dia"
                    ])

                df_raw = pd.concat([df, pd.DataFrame(novas_linhas)], ignore_index=True)
                df_raw["Dia"] = df_raw["Dia"].astype(str)

                df_somatoria = (
                    df_raw.groupby(["Dia", "Nome"], as_index=False)["Pontos na Rodada"]
                    .sum()
                    .rename(columns={"Pontos na Rodada": "Total no Dia"})
                )

                resumo_list, analytics_list = [], []
                for nome, group in df_raw.groupby("Nome"):
                    partidas = group["ID_Partida"].nunique()
                    rodadas = len(group)
                    pts_totais = group["Pontos na Rodada"].sum()
                    media = pts_totais / rodadas if rodadas > 0 else 0
                    vitorias = sum(group["Pontos na Rodada"] > 0)
                    resumo_list.append({
                        "Jogador": nome,
                        "Partidas Jogadas": int(partidas),
                        "Rodadas Jogadas": int(rodadas),
                        "Pontuação Total": int(pts_totais),
                        "Média de Pontos": round(float(media), 2),
                        "Vitórias em Rodadas": int(vitorias)
                    })

                    impostor_df = group[group["Papel"] == "Impostor"]
                    vz_imp = len(impostor_df)
                    fugas = len(impostor_df[impostor_df["Pontos na Rodada"] > 0])
                    taxa_fuga = (fugas / vz_imp * 100) if vz_imp > 0 else 0

                    inocente_df = group[group["Papel"] == "Inocente"]
                    vt_efetuados = inocente_df["Votos_Efetuados"].sum()
                    acertos = inocente_df["Acertos_Detetive"].sum()
                    taxa_acerto = (acertos / vt_efetuados * 100) if vt_efetuados > 0 else 0
                    bode = inocente_df["Votos_Recebidos"].sum()

                    analytics_list.append({
                        "Jogador": nome,
                        "Taxa de Fuga (%)": round(taxa_fuga, 2),
                        "Faro de Detetive (%)": round(taxa_acerto, 2),
                        "Votos Sofridos Sendo Inocente": int(bode),
                        "Título": "Membro Comum"
                    })

                df_resumo = pd.DataFrame(resumo_list)
                df_analytics = pd.DataFrame(analytics_list)

                if not df_analytics.empty:
                    mf = df_analytics["Taxa de Fuga (%)"].max()
                    md = df_analytics["Faro de Detetive (%)"].max()
                    mb = df_analytics["Votos Sofridos Sendo Inocente"].max()
                    for idx, row in df_analytics.iterrows():
                        titulos = []
                        if row["Taxa de Fuga (%)"] == mf and mf > 0:
                            titulos.append("Loki")
                        if row["Faro de Detetive (%)"] == md and md > 0:
                            titulos.append("Sherlock")
                        if row["Votos Sofridos Sendo Inocente"] == mb and mb > 0:
                            titulos.append("Bode Expiatório")
                        if titulos:
                            df_analytics.at[idx, "Título"] = " | ".join(titulos)

                imp_counts = (
                    df_raw[df_raw["Papel"] == "Impostor"]
                    .groupby("Nome")
                    .size()
                    .reset_index(name="Vezes_Impostor")
                    .rename(columns={"Nome": "Jogador"})
                )

                palavras_existentes = set()
                if insp.has_table("Palavras_Usadas"):
                    try:
                        df_p_db = pd.read_sql_table("Palavras_Usadas", conn)
                        if "Palavra" in df_p_db.columns:
                            palavras_existentes = set(df_p_db["Palavra"].dropna().astype(str).tolist())
                    except Exception:
                        pass

                todas_palavras = palavras_existentes.union(set(palavras_usadas))
                df_palavras = pd.DataFrame(sorted(todas_palavras), columns=["Palavra"])
                df_estado_eventos = pd.DataFrame([{
                    "Acumulado_Caos": int(acumulado_caos),
                    "Acumulado_Trapaca": int(acumulado_trapaca)
                }])

                df_raw.to_sql("Rodadas", conn, if_exists="replace", index=False)
                df_somatoria.to_sql("Somatoria_Diaria", conn, if_exists="replace", index=False)
                df_resumo.to_sql("Resumo", conn, if_exists="replace", index=False)
                df_analytics.to_sql("Analytics", conn, if_exists="replace", index=False)
                imp_counts.to_sql("Historico_Impostores", conn, if_exists="replace", index=False)
                df_palavras.to_sql("Palavras_Usadas", conn, if_exists="replace", index=False)
                df_estado_eventos.to_sql("Estado_Eventos", conn, if_exists="replace", index=False)
    except Exception as e:
        print("Erro ao salvar no banco:", e)


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
socketio = SocketIO(app, cors_allowed_origins="*")

salas_ativas = {}


def gerar_codigo_sala():
    while True:
        codigo = ''.join(random.choices(string.ascii_uppercase, k=4))
        if codigo not in salas_ativas:
            return codigo


def calcular_tempo_restante(sala):
    fim = sala.get('debate_fim_ts', 0)
    if not fim:
        return sala.get('tempo_discussao', 120)
    return max(0, int(round(fim - time.time())))


def sortear_palavra_categoria(sala, categoria):
    opcoes = [
        p for p in BANCO_PALAVRAS[categoria]
        if chave_palavra(categoria, p[0]) not in sala['palavras_usadas']
    ]
    if not opcoes:
        opcoes = BANCO_PALAVRAS[categoria]
        prefixo = f"{categoria}||"
        sala['palavras_usadas'] = {
            u for u in sala['palavras_usadas'] if not str(u).startswith(prefixo)
        }
    palavra_secreta, dica_vaga = random.choice(opcoes)
    sala['palavras_usadas'].add(chave_palavra(categoria, palavra_secreta))
    return palavra_secreta, dica_vaga


def heartbeat_sala(codigo):
    while codigo in salas_ativas:
        socketio.sleep(1.5)
        sala = salas_ativas.get(codigo)
        if not sala:
            break

        if time.time() - sala.get('ultima_atividade', time.time()) > TEMPO_EXPIRACAO_SALA:
            socketio.emit('sala_destruida', {}, to=codigo)
            salas_ativas.pop(codigo, None)
            break

        estado_reduzido = {
            'fase': sala['fase_atual'],
            'iteracao': sala['iteracao_fase']
        }
        socketio.emit('sync_estado', estado_reduzido, to=codigo)
        socketio.emit('sync_estado_host', estado_reduzido, to=codigo)


def montar_estado_host(codigo, sala):
    return {
        'codigo': codigo,
        'fase_atual': sala['fase_atual'],
        'iteracao': sala['iteracao_fase'],
        'jogadores': [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']],
        'temas': list(BANCO_PALAVRAS.keys()),
        'modo_jogo': sala.get('modo_jogo', 'host'),
        'primeiro_falar': sala.get('primeiro_falar', ''),
        'votos_computados': len(sala.get('votos', {})),
        'total_jogadores': len(sala['jogadores']),
        'ultimo_resultado': sala.get('ultimo_resultado', {}),
        'prontos_computados': len(sala.get('jogadores_prontos', set())),
        'tempo_total': sala.get('tempo_discussao', 120),
        'tempo_restante': calcular_tempo_restante(sala)
    }


def enviar_estado_jogador(sala, jogador):
    fase = sala.get('fase_atual')
    sid = jogador['sid']

    if fase == 'revelacao':
        eh_impostor = jogador['nome'] in sala.get('impostores_atuais', [])
        payload = {
            'fase': fase,
            'iteracao': sala['iteracao_fase'],
            'papel': 'impostor' if eh_impostor else 'inocente',
            'tema': sala.get('tema_atual', 'Geral'),
            'caos_ativo': sala.get('caos_ativo_atual', False) if eh_impostor else False,
            'ja_confirmou': jogador['nome'] in sala.get('jogadores_prontos', set()),
            'ja_votou_ja_foi': jogador['nome'] in sala.get('jogadores_ja_foi', set()),
            'ja_foi_clicados': len(sala.get('jogadores_ja_foi', set())),
            'total_jogadores': len(sala['jogadores'])
        }
        if eh_impostor:
            payload['palavra_ou_dica'] = sala.get('dica_vaga', '')
            if sala.get('trapaca_ativo_atual', False):
                payload['equipe'] = [i for i in sala.get('impostores_atuais', []) if i != jogador['nome']]
        else:
            payload['palavra_ou_dica'] = sala.get('palavra_atual', '')

        socketio.emit('distribuir_papeis', payload, to=sid)

    elif fase == 'debate':
        socketio.emit('todos_leram_papeis_global', {
            'fase': 'debate',
            'iteracao': sala['iteracao_fase'],
            'tempo': calcular_tempo_restante(sala),
            'tempo_total': sala.get('tempo_discussao', 120),
            'primeiro': sala.get('primeiro_falar', '---')
        }, to=sid)

    elif fase == 'votacao':
        jogadores_info = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        ja_votou = jogador['nome'] in sala.get('votos', {})
        socketio.emit('ir_para_tela_votacao', {
            'fase': fase,
            'iteracao': sala['iteracao_fase'],
            'jogadores': jogadores_info,
            'ja_votou': ja_votou,
            'qtd_votos': max(1, len(sala.get('impostores_atuais', []))),
            'caos_geral': sala.get('caos_ativo_atual', False)
        }, to=sid)

    elif fase == 'resultado':
        if sala.get('ultimo_resultado'):
            res = dict(sala['ultimo_resultado'])
            res['fase'] = fase
            res['iteracao'] = sala['iteracao_fase']
            socketio.emit('resultado_votacao_global', res, to=sid)
        else:
            socketio.emit('votacao_encerrada_celular', {
                'fase': fase,
                'iteracao': sala['iteracao_fase']
            }, to=sid)

    elif fase == 'lobby':
        socketio.emit('retorno_lobby_celular', {
            'fase': fase,
            'iteracao': sala['iteracao_fase'],
            'jogadores': [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        }, to=sid)


def checar_todos_leram(codigo):
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'revelacao':
        return

    total_prontos = len(sala['jogadores_prontos'])
    total_jogadores = len(sala['jogadores'])

    socketio.emit('progresso_leitura_host', {
        'prontos': total_prontos,
        'total': total_jogadores
    }, to=codigo)

    if total_jogadores > 0 and total_prontos >= total_jogadores:
        sala['fase_atual'] = 'debate'
        sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
        sala['debate_iniciado'] = True
        sala['debate_fim_ts'] = time.time() + sala['tempo_discussao']
        registrar_atividade(sala)

        payload_debate = {
            'fase': 'debate',
            'iteracao': sala['iteracao_fase'],
            'tempo': sala['tempo_discussao'],
            'tempo_total': sala['tempo_discussao'],
            'primeiro': sala.get('primeiro_falar', '---')
        }
        socketio.emit('todos_leram_papeis', payload_debate, to=codigo)
        socketio.emit('todos_leram_papeis_global', payload_debate, to=codigo)


def checar_avanco_ja_foi(codigo):
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'revelacao':
        return

    if len(sala['jogadores']) > 0 and len(sala['jogadores_ja_foi']) >= len(sala['jogadores']):
        sala['jogadores_ja_foi'] = set()
        sala['jogadores_prontos'] = set()
        sala['confirmacoes_status'] = set()
        sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
        sala['debate_iniciado'] = False
        sala['debate_fim_ts'] = 0
        registrar_atividade(sala)

        categoria = sala['tema_atual']
        nova_palavra, nova_dica = sortear_palavra_categoria(sala, categoria)
        sala['palavra_atual'] = nova_palavra
        sala['dica_vaga'] = nova_dica

        for jogador in sala['jogadores']:
            enviar_estado_jogador(sala, jogador)

        socketio.emit('progresso_leitura_host', {
            'prontos': 0,
            'total': len(sala['jogadores'])
        }, to=codigo)
        socketio.emit('travar_timer_host', {
            'fase': 'revelacao',
            'iteracao': sala['iteracao_fase']
        }, to=codigo)


def encerrar_votacao_interna(codigo):
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] == 'resultado':
        return

    sala['fase_atual'] = 'resultado'
    sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    registrar_atividade(sala)

    votos = sala.get('votos', {})
    total_inocentes_mesa = sum(1 for j in sala['jogadores'] if j['nome'] not in sala['impostores_atuais'])
    limite_para_pegar = max(1, total_inocentes_mesa / 2.0)

    contagem = {}
    for eleitor, lista_votados in votos.items():
        if eleitor not in sala['impostores_atuais'] and isinstance(lista_votados, list):
            for votado in lista_votados:
                contagem[votado] = contagem.get(votado, 0) + 1

    eliminados_de_fato = [k for k, v in contagem.items() if v >= limite_para_pegar]

    pontos_da_rodada = {}
    detalhes_rodada = {}
    for j in sala['jogadores']:
        nome = j['nome']
        eh_impostor = nome in sala['impostores_atuais']
        votos_recebidos = contagem.get(nome, 0)
        votos_dados = votos.get(nome) or []
        acertos_detetive = sum(1 for v in votos_dados if v in sala['impostores_atuais']) if not eh_impostor else 0

        pts = 0
        if eh_impostor:
            if nome not in eliminados_de_fato:
                pts = 2
        else:
            if acertos_detetive > 0:
                pts = acertos_detetive

        pontos_da_rodada[nome] = pts
        sala['placar'][nome] = sala['placar'].get(nome, 0) + pts
        detalhes_rodada[nome] = {
            "Papel": "Impostor" if eh_impostor else "Inocente",
            "Votos_Recebidos": votos_recebidos,
            "Acertos_Detetive": acertos_detetive,
            "Votos_Efetuados": len(votos_dados)
        }

    socketio.start_background_task(
        salvar_banco_dados,
        dict(pontos_da_rodada),
        dict(sala['placar']),
        sala['id_partida'],
        dict(detalhes_rodada),
        set(sala['palavras_usadas']),
        sala['acumulado_caos'],
        sala['acumulado_trapaca']
    )

    destaques_resultado = []
    for imp in sala['impostores_atuais']:
        destaques_resultado.append({
            'nome': imp,
            'emoji': next((j['emoji'] for j in sala['jogadores'] if j['nome'] == imp), "👤"),
            'papel': 'Impostor',
            'status': 'Foi pego!' if imp in eliminados_de_fato else 'Conseguiu fugir!'
        })

    for elim in eliminados_de_fato:
        if elim not in sala['impostores_atuais']:
            destaques_resultado.append({
                'nome': elim,
                'emoji': next((j['emoji'] for j in sala['jogadores'] if j['nome'] == elim), "👤"),
                'papel': 'Inocente',
                'status': 'Executado por engano!'
            })

    outros_votados = [
        {
            'nome': nome,
            'emoji': next((j['emoji'] for j in sala['jogadores'] if j['nome'] == nome), "👤"),
            'votos': qtd
        }
        for nome, qtd in sorted(contagem.items(), key=lambda x: x[1], reverse=True)
        if nome not in sala['impostores_atuais'] and nome not in eliminados_de_fato and qtd > 0
    ]

    payload_resultado = {
        'destaques': destaques_resultado,
        'outros_votados': outros_votados,
        'palavra_atual': sala['palavra_atual'],
        'pontos': dict(sala['placar']),
        'iteracao': sala['iteracao_fase'],
        'fase': 'resultado'
    }

    sala['ultimo_resultado'] = payload_resultado

    socketio.emit('resultado_votacao_global', payload_resultado, to=codigo)
    socketio.emit('resultado_votacao_host', payload_resultado, to=codigo)


@app.route('/')
def tela_jogador():
    return render_template('jogador.html')


@app.route('/host')
def tela_admin():
    return render_template('admin.html')


@app.route('/testador')
def tela_testador():
    return render_template('testador.html')


@socketio.on('validar_senha_admin')
def validar_senha_admin(dados):
    if autorizado_admin(dados):
        emit('admin_autenticado', {'ok': True}, to=request.sid)
    else:
        emit('alerta_host', {'mensagem': 'Senha de administrador incorreta!'}, to=request.sid)


@socketio.on('pedir_sync_jogador')
def pedir_sync_jogador(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    nome = (dados.get('nome') or '').strip()
    sala = salas_ativas.get(codigo)
    if not sala:
        emit('sala_destruida', {}, to=request.sid)
        return
    jogador = encontrar_jogador(sala, nome)
    if jogador:
        jogador['sid'] = request.sid
        join_room(codigo)
        enviar_estado_jogador(sala, jogador)


@socketio.on('pedir_sync_host')
def pedir_sync_host(dados):
    if not isinstance(dados, dict):
        return
    verificar_sala_existente(dados)


@socketio.on('confirmar_status')
def confirmar_status(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    nome = (dados.get('nome') or '').strip()
    status = dados.get('status')
    sala = salas_ativas.get(codigo)
    if sala and sala.get('fase_atual') == status:
        jogador = encontrar_jogador(sala, nome)
        if jogador:
            jogador['sid'] = request.sid
            if 'confirmacoes_status' not in sala:
                sala['confirmacoes_status'] = set()
            sala['confirmacoes_status'].add(jogador['nome'])


@socketio.on('verificar_sala_existente')
def verificar_sala_existente(dados):
    codigo = (dados.get('codigo') or '').strip().upper() if isinstance(dados, dict) else ''
    sala = salas_ativas.get(codigo) if codigo else None

    if sala and autorizado_host(sala, dados):
        sala['host_sid'] = request.sid
        join_room(codigo)
        registrar_atividade(sala)

        nome_host = (dados.get('nome_host') or '').strip()
        jogador_host = encontrar_jogador(sala, nome_host) if nome_host else None
        if jogador_host:
            jogador_host['sid'] = request.sid

        emit('sala_recuperada', montar_estado_host(codigo, sala), to=request.sid)
        if jogador_host and sala['fase_atual'] in ('revelacao', 'votacao'):
            enviar_estado_jogador(sala, jogador_host)
    else:
        emit('nenhuma_sala_ativa', {}, to=request.sid)


@socketio.on('criar_sala')
def criar_sala():
    codigo = gerar_codigo_sala()
    host_token = secrets.token_hex(16)
    salas_ativas[codigo] = {
        'host_sid': request.sid,
        'host_token': host_token,
        'id_partida': str(uuid.uuid4())[:8],
        'jogadores': [],
        'historico_impostores': {},
        'palavras_usadas': set(),
        'acumulado_caos': 10,
        'acumulado_trapaca': 20,
        'impostores_ultima_rodada': [],
        'placar': {},
        'fila_interrogatorio': [],
        'fase_atual': 'lobby',
        'iteracao_fase': 0,
        'jogadores_prontos': set(),
        'jogadores_ja_foi': set(),
        'impostores_atuais': [],
        'palavra_atual': '',
        'tema_atual': '',
        'dica_vaga': '',
        'tempo_discussao': 120,
        'debate_fim_ts': 0,
        'votos': {},
        'modo_jogo': 'host',
        'debate_iniciado': False,
        'ultimo_resultado': {},
        'ultima_atividade': time.time()
    }
    carregar_dados_sala_do_banco(salas_ativas[codigo])
    join_room(codigo)
    socketio.start_background_task(heartbeat_sala, codigo)
    emit('sala_criada_sucesso', {
        'codigo': codigo,
        'host_token': host_token,
        'temas': list(BANCO_PALAVRAS.keys())
    }, to=request.sid)


@socketio.on('atualizar_modo_jogo')
def atualizar_modo_jogo(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or not autorizado_host(sala, dados):
        return
    modo = dados.get('modo_jogo')
    if modo in ('host', 'host_jogador'):
        sala['modo_jogo'] = modo
        registrar_atividade(sala)


@socketio.on('destruir_sala')
def destruir_sala(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala:
        emit('sala_destruida', {}, to=request.sid)
        return
    if not autorizado_host(sala, dados):
        emit('alerta_host', {'mensagem': 'Sem permissão para encerrar esta sala.'}, to=request.sid)
        return
    socketio.emit('sala_destruida', {}, to=codigo)
    salas_ativas.pop(codigo, None)


@socketio.on('forcar_avanco')
def forcar_avanco(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or not autorizado_host(sala, dados):
        emit('alerta_host', {'mensagem': 'Sem permissão para forçar avanço.'}, to=request.sid)
        return

    registrar_atividade(sala)
    if sala['fase_atual'] == 'revelacao':
        sala['jogadores_prontos'] = set(j['nome'] for j in sala['jogadores'])
        checar_todos_leram(codigo)
    elif sala['fase_atual'] == 'debate':
        iniciar_votacao(dados)
    elif sala['fase_atual'] == 'votacao':
        encerrar_votacao_interna(codigo)


@socketio.on('solicitar_coroacao')
def solicitar_coroacao(dados):
    tipo = dados.get('tipo') if isinstance(dados, dict) else 'Geral'
    emit('receber_coroacao', {
        'tipo': tipo,
        'ranking': obter_ranking_dinamico(tipo)
    }, to=request.sid)


@socketio.on('deletar_jogador_banco')
def deletar_jogador_banco(dados):
    if not autorizado_admin(dados):
        emit('alerta_host', {'mensagem': 'Senha de administrador incorreta!'}, to=request.sid)
        return

    nome = (dados.get('nome') or '').strip()
    if not nome:
        return
    try:
        with db_lock:
            with engine.begin() as conn:
                insp = inspect(conn)
                if insp.has_table("Rodadas"):
                    conn.execute(text('DELETE FROM "Rodadas" WHERE "Nome" = :n'), {"n": nome})
                if insp.has_table("Somatoria_Diaria"):
                    conn.execute(text('DELETE FROM "Somatoria_Diaria" WHERE "Nome" = :n'), {"n": nome})
                if insp.has_table("Resumo"):
                    conn.execute(text('DELETE FROM "Resumo" WHERE "Jogador" = :n'), {"n": nome})
                if insp.has_table("Analytics"):
                    conn.execute(text('DELETE FROM "Analytics" WHERE "Jogador" = :n'), {"n": nome})
                if insp.has_table("Historico_Impostores"):
                    conn.execute(text('DELETE FROM "Historico_Impostores" WHERE "Jogador" = :n'), {"n": nome})

        for sala in salas_ativas.values():
            for k in list(sala['historico_impostores'].keys()):
                if k.lower() == nome.lower():
                    del sala['historico_impostores'][k]
            for k in list(sala['placar'].keys()):
                if k.lower() == nome.lower():
                    del sala['placar'][k]

        emit('alerta_host', {'mensagem': f'Todos os dados de {nome} foram apagados com sucesso!'}, to=request.sid)
    except Exception as e:
        emit('alerta_host', {'mensagem': f'Erro ao deletar jogador: {str(e)}'}, to=request.sid)


@socketio.on('resetar_banco')
def resetar_banco(dados):
    if not autorizado_admin(dados):
        emit('alerta_host', {'mensagem': 'Senha de administrador incorreta!'}, to=request.sid)
        return

    tipo = dados.get('tipo')
    try:
        with db_lock:
            with engine.begin() as conn:
                insp = inspect(conn)
                if tipo in ['palavras', 'tudo']:
                    if insp.has_table("Palavras_Usadas"):
                        conn.execute(text('DELETE FROM "Palavras_Usadas"'))
                    for sala in salas_ativas.values():
                        sala['palavras_usadas'] = set()
                if tipo in ['partidas', 'tudo']:
                    for t in ["Rodadas", "Somatoria_Diaria", "Resumo", "Analytics", "Historico_Impostores", "Estado_Eventos"]:
                        if insp.has_table(t):
                            conn.execute(text(f'DELETE FROM "{t}"'))
                    for sala in salas_ativas.values():
                        sala['historico_impostores'] = {}
                        sala['acumulado_caos'] = 10
                        sala['acumulado_trapaca'] = 20
                        sala['placar'] = {}
                        sala['impostores_ultima_rodada'] = []
        emit('alerta_host', {'mensagem': 'Operação concluída com sucesso! Banco atualizado.'}, to=request.sid)
    except Exception as e:
        emit('alerta_host', {'mensagem': f'Erro ao processar o reset: {str(e)}'}, to=request.sid)


@socketio.on('entrar_na_sala')
def entrar_na_sala(dados):
    if not isinstance(dados, dict):
        return
    nome = (dados.get('nome') or '').strip()
    codigo = (dados.get('codigo') or '').strip().upper()

    if not nome or codigo not in salas_ativas:
        emit('erro_conexao', {'mensagem': 'Sala não encontrada! A partida foi encerrada ou o código está incorreto.'}, to=request.sid)
        return

    sala = salas_ativas[codigo]
    registrar_atividade(sala)
    jogador_existente = encontrar_jogador(sala, nome)

    if jogador_existente:
        jogador_existente['sid'] = request.sid
        join_room(codigo)
        emit('entrada_sucesso', {
            'nome': jogador_existente['nome'],
            'reconexao': True,
            'fase': sala['fase_atual']
        }, to=request.sid)
        lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        socketio.emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
        enviar_estado_jogador(sala, jogador_existente)
        return

    if sala['fase_atual'] != 'lobby':
        emit('erro_conexao', {'mensagem': 'A partida já começou! Você não pode entrar no meio do jogo.'}, to=request.sid)
        return

    if nome not in sala['historico_impostores']:
        sala['historico_impostores'][nome] = 0
    emojis_usados = [j['emoji'] for j in sala['jogadores']]
    opcoes = [e for e in EMOJIS_DISPONIVEIS if e not in emojis_usados]
    meu_emoji = random.choice(opcoes if opcoes else EMOJIS_DISPONIVEIS)

    join_room(codigo)
    novo_jogador = {'sid': request.sid, 'nome': nome, 'emoji': meu_emoji}
    sala['jogadores'].append(novo_jogador)
    lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
    emit('entrada_sucesso', {'nome': nome, 'reconexao': False, 'fase': 'lobby'}, to=request.sid)
    socketio.emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)
    enviar_estado_jogador(sala, novo_jogador)


@socketio.on('sair_da_sala')
def sair_da_sala(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    nome_sair = (dados.get('nome') or '').strip()
    sala = salas_ativas.get(codigo)
    if not sala:
        return

    jogador = encontrar_jogador(sala, nome_sair)
    if jogador:
        nome_real = jogador['nome']
        sala['jogadores'].remove(jogador)
        registrar_atividade(sala)
        lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        socketio.emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)

        if sala['fase_atual'] == 'revelacao':
            sala['jogadores_prontos'].discard(nome_real)
            sala['jogadores_ja_foi'].discard(nome_real)
            checar_avanco_ja_foi(codigo)
            checar_todos_leram(codigo)
        elif sala['fase_atual'] == 'votacao':
            sala['votos'].pop(nome_real, None)
            socketio.emit('voto_recebido_host', {
                'total_votos': len(sala['votos']),
                'total_jogadores': len(sala['jogadores'])
            }, to=codigo)
            if len(sala['jogadores']) > 0 and len(sala['votos']) >= len(sala['jogadores']):
                encerrar_votacao_interna(codigo)


@socketio.on('trocar_emoji')
def trocar_emoji(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    nome = (dados.get('nome') or '').strip()
    novo_emoji = dados.get('emoji')
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'lobby':
        return

    emojis_em_uso = [j['emoji'] for j in sala['jogadores']]
    if novo_emoji not in emojis_em_uso and novo_emoji in EMOJIS_DISPONIVEIS:
        jogador = encontrar_jogador(sala, nome)
        if jogador:
            jogador['sid'] = request.sid
            jogador['emoji'] = novo_emoji
            registrar_atividade(sala)
            lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
            socketio.emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)


@socketio.on('expulsar_jogador')
def expulsar_jogador(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    nome_expulso = (dados.get('nome') or '').strip()
    sala = salas_ativas.get(codigo)
    if not sala or not autorizado_host(sala, dados):
        emit('alerta_host', {'mensagem': 'Sem permissão para expulsar jogadores.'}, to=request.sid)
        return

    jogador = encontrar_jogador(sala, nome_expulso)
    if jogador:
        nome_real = jogador['nome']
        sala['jogadores'].remove(jogador)
        registrar_atividade(sala)
        if jogador['sid'] != sala.get('host_sid'):
            socketio.emit('foi_expulso', {}, to=jogador['sid'])
        lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
        socketio.emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)

        if sala['fase_atual'] == 'revelacao':
            sala['jogadores_prontos'].discard(nome_real)
            sala['jogadores_ja_foi'].discard(nome_real)
            checar_avanco_ja_foi(codigo)
            checar_todos_leram(codigo)
        elif sala['fase_atual'] == 'votacao':
            sala['votos'].pop(nome_real, None)
            socketio.emit('voto_recebido_host', {
                'total_votos': len(sala['votos']),
                'total_jogadores': len(sala['jogadores'])
            }, to=codigo)
            if len(sala['jogadores']) > 0 and len(sala['votos']) >= len(sala['jogadores']):
                encerrar_votacao_interna(codigo)


@socketio.on('voltar_para_lobby')
def voltar_para_lobby(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or not autorizado_host(sala, dados):
        emit('alerta_host', {'mensagem': 'Sem permissão para voltar ao lobby.'}, to=request.sid)
        return

    sala['fase_atual'] = 'lobby'
    sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    sala['votos'] = {}
    sala['jogadores_prontos'] = set()
    sala['jogadores_ja_foi'] = set()
    sala['debate_iniciado'] = False
    sala['debate_fim_ts'] = 0
    registrar_atividade(sala)

    lista_atualizada = [{'nome': j['nome'], 'emoji': j['emoji']} for j in sala['jogadores']]
    socketio.emit('retorno_lobby_host', {
        'fase': 'lobby',
        'iteracao': sala['iteracao_fase'],
        'jogadores': lista_atualizada
    }, to=codigo)
    socketio.emit('retorno_lobby_celular', {
        'fase': 'lobby',
        'iteracao': sala['iteracao_fase'],
        'jogadores': lista_atualizada
    }, to=codigo)
    socketio.emit('lista_jogadores_atualizada', {'jogadores': lista_atualizada}, to=codigo)


@socketio.on('iniciar_partida')
def iniciar_partida(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or not autorizado_host(sala, dados):
        emit('alerta_host', {'mensagem': 'Sem permissão para iniciar a partida.'}, to=request.sid)
        return
    if len(sala['jogadores']) < 3:
        return

    sala['modo_jogo'] = dados.get('modo_jogo', sala.get('modo_jogo', 'host'))
    sala['fase_atual'] = 'revelacao'
    sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    sala['jogadores_prontos'] = set()
    sala['jogadores_ja_foi'] = set()
    sala['votos'] = {}
    sala['tempo_discussao'] = max(30, int(dados.get('tempo') or 120))
    sala['debate_iniciado'] = False
    sala['debate_fim_ts'] = 0
    registrar_atividade(sala)

    categoria = dados.get('categoria', 'Aleatório')
    if categoria == "Aleatório" or categoria not in BANCO_PALAVRAS:
        categoria = random.choice(list(BANCO_PALAVRAS.keys()))

    palavra_secreta, dica_vaga = sortear_palavra_categoria(sala, categoria)

    prob_caos = max(0, min(100, int(dados.get('caos') or 10)))
    prob_trapaca = max(0, min(100, int(dados.get('trapaca') or 20)))
    imp_base = max(1, int(dados.get('imp_base') or 1))

    caos_ativo = False
    if len(sala['jogadores']) >= 3:
        caos_ativo = random.random() < (sala['acumulado_caos'] / 100.0)
        if caos_ativo:
            sala['acumulado_caos'] = prob_caos
        else:
            sala['acumulado_caos'] = min(100, sala['acumulado_caos'] + prob_caos)

    qtd_impostores = min(len(sala['jogadores']) - 1, imp_base + (1 if caos_ativo else 0))

    trapaca_ativo = False
    if qtd_impostores > 1:
        trapaca_ativo = random.random() < (sala['acumulado_trapaca'] / 100.0)
        if trapaca_ativo:
            sala['acumulado_trapaca'] = prob_trapaca
        else:
            sala['acumulado_trapaca'] = min(100, sala['acumulado_trapaca'] + prob_trapaca)

    candidatos = [j['nome'] for j in sala['jogadores']]
    pesos = [
        max(0.01, 1.0 - (0.33 * min(sala['historico_impostores'].get(j, 0), 3)))
        * (0.5 if j in sala['impostores_ultima_rodada'] else 1.0)
        for j in candidatos
    ]

    impostores_sorteados = []
    for _ in range(qtd_impostores):
        escolhido = random.choices(candidatos, weights=pesos, k=1)[0]
        impostores_sorteados.append(escolhido)
        idx = candidatos.index(escolhido)
        candidatos.pop(idx)
        pesos.pop(idx)

    sala['impostores_ultima_rodada'] = list(impostores_sorteados)
    for imp in impostores_sorteados:
        sala['historico_impostores'][imp] = sala['historico_impostores'].get(imp, 0) + 1
    if sum(1 for c in sala['historico_impostores'].values() if c >= 3) >= 3:
        for k in sala['historico_impostores']:
            sala['historico_impostores'][k] = 0

    sala['impostores_atuais'] = impostores_sorteados
    sala['palavra_atual'] = palavra_secreta
    sala['tema_atual'] = categoria
    sala['dica_vaga'] = dica_vaga
    sala['caos_ativo_atual'] = caos_ativo
    sala['trapaca_ativo_atual'] = trapaca_ativo

    primeiro = random.choice(sala['jogadores'])
    sala['primeiro_falar'] = f"{primeiro['emoji']} {primeiro['nome']}"

    for jogador in sala['jogadores']:
        enviar_estado_jogador(sala, jogador)

    socketio.emit('partida_iniciada_host', {
        'fase': 'revelacao',
        'primeiro': sala['primeiro_falar'],
        'total_jogadores': len(sala['jogadores']),
        'iteracao': sala['iteracao_fase']
    }, to=codigo)


@socketio.on('clicou_ja_foi')
def clicou_ja_foi(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'revelacao':
        return

    jogador = encontrar_jogador(sala, dados.get('nome'))
    if jogador:
        jogador['sid'] = request.sid
        sala['jogadores_ja_foi'].add(jogador['nome'])
        registrar_atividade(sala)
        socketio.emit('atualizar_contagem_ja_foi', {
            'clicados': len(sala['jogadores_ja_foi']),
            'total': len(sala['jogadores'])
        }, to=codigo)
        checar_avanco_ja_foi(codigo)


@socketio.on('confirmar_leitura_papel')
def confirmar_leitura_papel(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'revelacao':
        return

    jogador = encontrar_jogador(sala, dados.get('nome'))
    if jogador:
        jogador['sid'] = request.sid
        sala['jogadores_prontos'].add(jogador['nome'])
        registrar_atividade(sala)
        checar_todos_leram(codigo)


@socketio.on('iniciar_votacao')
def iniciar_votacao(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or not autorizado_host(sala, dados):
        emit('alerta_host', {'mensagem': 'Sem permissão para iniciar a votação.'}, to=request.sid)
        return
    if sala['fase_atual'] not in ('revelacao', 'debate'):
        return

    sala['fase_atual'] = 'votacao'
    sala['iteracao_fase'] = sala.get('iteracao_fase', 0) + 1
    sala['votos'] = {}
    sala['debate_fim_ts'] = 0
    registrar_atividade(sala)

    for jogador in sala['jogadores']:
        enviar_estado_jogador(sala, jogador)

    socketio.emit('votacao_iniciada_host', {
        'fase': 'votacao',
        'total_jogadores': len(sala['jogadores']),
        'iteracao': sala['iteracao_fase']
    }, to=codigo)


@socketio.on('enviar_voto')
def receber_voto(dados):
    if not isinstance(dados, dict):
        return
    codigo = (dados.get('codigo') or '').strip().upper()
    sala = salas_ativas.get(codigo)
    if not sala or sala['fase_atual'] != 'votacao':
        emit('erro_voto', {'mensagem': 'A votação não está ativa no momento.'}, to=request.sid)
        return

    jogador = encontrar_jogador(sala, dados.get('nome'))
    if not jogador:
        emit('erro_voto', {'mensagem': 'Jogador não encontrado na sala.'}, to=request.sid)
        return

    jogador['sid'] = request.sid
    join_room(codigo)
    registrar_atividade(sala)

    votos_brutos = dados.get('voto')
    if not isinstance(votos_brutos, list):
        emit('erro_voto', {'mensagem': 'Formato de voto inválido.'}, to=request.sid)
        return

    nomes_validos = {j['nome'].lower(): j['nome'] for j in sala['jogadores'] if j['nome'] != jogador['nome']}
    votos_limpos = []
    for v in votos_brutos:
        alvo = nomes_validos.get(str(v).strip().lower())
        if alvo and alvo not in votos_limpos:
            votos_limpos.append(alvo)

    qtd_esperada = max(1, len(sala.get('impostores_atuais', [])))
    if len(votos_limpos) != qtd_esperada:
        emit('erro_voto', {'mensagem': f'Selecione exatamente {qtd_esperada} suspeito(s).'}, to=request.sid)
        return

    sala['votos'][jogador['nome']] = votos_limpos
    emit('voto_confirmado', {
        'iteracao': sala['iteracao_fase'],
        'nome': jogador['nome']
    }, to=request.sid)

    socketio.emit('voto_recebido_host', {
        'total_votos': len(sala['votos']),
        'total_jogadores': len(sala['jogadores'])
    }, to=codigo)

    if len(sala['votos']) >= len(sala['jogadores']):
        encerrar_votacao_interna(codigo)


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=8080)
