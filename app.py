import random
import string
from flask import Flask, render_template, request
from flask_socketio import SocketIO, join_room, emit

# Importa o seu banco de palavras do outro arquivo
from banco_palavras import BANCO_PALAVRAS

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

@socketio.on('connect')
def handle_connect():
    pass

@socketio.on('criar_sala')
def criar_sala():
    codigo = gerar_codigo_sala()
    salas_ativas[codigo] = {
        'host_sid': request.sid,
        'jogadores': [],
        # Memória da Partida
        'historico_impostores': {},
        'palavras_usadas': set(),
        'acumulado_caos': 10,  # Valores padrão
        'acumulado_trapaca': 20
    }
    join_room(codigo)
    emit('sala_criada_sucesso', {'codigo': codigo})

@socketio.on('entrar_na_sala')
def entrar_na_sala(dados):
    nome = dados.get('nome', '').strip()
    codigo = dados.get('codigo', '').strip().upper()

    if codigo in salas_ativas:
        # Garante que o jogador inicie com histórico 0 se for a primeira vez
        if nome not in salas_ativas[codigo]['historico_impostores']:
            salas_ativas[codigo]['historico_impostores'][nome] = 0

        join_room(codigo)
        salas_ativas[codigo]['jogadores'].append({'sid': request.sid, 'nome': nome})
        
        lista_nomes = [j['nome'] for j in salas_ativas[codigo]['jogadores']]
        emit('lista_jogadores_atualizada', {'jogadores': lista_nomes}, to=codigo)
        emit('entrada_sucesso', {'nome': nome})
    else:
        emit('erro_conexao', {'mensagem': 'Sala não encontrada!'})

@socketio.on('iniciar_partida')
def iniciar_partida(dados):
    codigo = dados.get('codigo')
    sala = salas_ativas.get(codigo)
    if not sala: return

    # 1. Puxa as configurações enviadas pelo Host (TV)
    categoria = dados.get('categoria')
    imp_base = int(dados.get('imp_base', 1))
    prob_caos = int(dados.get('caos', 10))
    prob_trapaca = int(dados.get('trapaca', 20))

    # Nivelamento do PRD se o Host mudou o slider
    sala['acumulado_caos'] = max(sala['acumulado_caos'], prob_caos)
    sala['acumulado_trapaca'] = max(sala['acumulado_trapaca'], prob_trapaca)

    # 2. Sorteio da Palavra
    if categoria == "Aleatório":
        categoria = random.choice(list(BANCO_PALAVRAS.keys()))
    
    opcoes = [p for p in BANCO_PALAVRAS[categoria] if p[0] not in sala['palavras_usadas']]
    if not opcoes:
        opcoes = BANCO_PALAVRAS[categoria]
        sala['palavras_usadas'].difference_update({p[0] for p in BANCO_PALAVRAS[categoria]})

    palavra_secreta, dica_vaga = random.choice(opcoes)
    sala['palavras_usadas'].add(palavra_secreta)

    # 3. Lógica do PRD (Caos e Trapaça)
    caos_ativo = random.random() < (sala['acumulado_caos'] / 100.0)
    trapaca_ativo = random.random() < (sala['acumulado_trapaca'] / 100.0)

    if caos_ativo: sala['acumulado_caos'] = prob_caos
    else: sala['acumulado_caos'] = min(100, sala['acumulado_caos'] + prob_caos)

    if trapaca_ativo: sala['acumulado_trapaca'] = prob_trapaca
    else: sala['acumulado_trapaca'] = min(100, sala['acumulado_trapaca'] + prob_trapaca)

    # 4. Sorteio de Papéis
    qtd_impostores = min(len(sala['jogadores']) - 1, imp_base + (1 if caos_ativo else 0))
    
    candidatos = [j['nome'] for j in sala['jogadores']]
    pesos = [max(0.01, 1.0 - (0.33 * min(sala['historico_impostores'][n], 3))) for n in candidatos]
    
    impostores_sorteados = []
    for _ in range(qtd_impostores):
        escolhido = random.choices(candidatos, weights=pesos, k=1)[0]
        impostores_sorteados.append(escolhido)
        idx = candidatos.index(escolhido)
        candidatos.pop(idx); pesos.pop(idx)

    # Atualiza o histórico
    for imp in impostores_sorteados:
        sala['historico_impostores'][imp] += 1
    if sum(1 for c in sala['historico_impostores'].values() if c >= 3) >= 3:
        for k in sala['historico_impostores']: sala['historico_impostores'][k] = 0

    # 5. Entrega os papéis secretamente para cada Celular via SID
    for jogador in sala['jogadores']:
        eh_impostor = jogador['nome'] in impostores_sorteados
        
        payload = {
            'papel': 'impostor' if eh_impostor else 'inocente',
            'tema': categoria,
            'caos_ativo': caos_ativo
        }

        if eh_impostor:
            payload['palavra_ou_dica'] = dica_vaga
            if trapaca_ativo:
                aliados = [i for i in impostores_sorteados if i != jogador['nome']]
                payload['equipe'] = aliados
        else:
            payload['palavra_ou_dica'] = palavra_secreta

        # Manda o pacote SÓ para o celular deste jogador
        emit('distribuir_papeis', payload, to=jogador['sid'])

    # Avisa a TV que a rodada começou (para trocar de tela)
    emit('partida_iniciada_host', {'msg': 'ok'}, to=sala['host_sid'])

if __name__ == '__main__':
    print("🚀 Iniciando Servidor Leoposter Online...")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)