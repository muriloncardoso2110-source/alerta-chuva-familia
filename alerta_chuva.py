
import os
import json
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# CONFIGURACAO
LOCAL = "Sao Miguel Paulista - SP"
LATITUDE = -23.49
LONGITUDE = -46.44
FUSO = ZoneInfo("America/Sao_Paulo")

CHANCE_MINIMA = 55
CHUVA_MINIMA = 0.2
INTERVALO_ALERTAS_HORAS = 3

ARQUIVO_ESTADO = Path("estado.json")


def agora():
    return datetime.now(FUSO)


def consultar_api(url, parametros=None):
    if parametros:
        url += "?" + urllib.parse.urlencode(parametros)

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "AlertaChuvaFamilia/1.0"}
    )

    with urllib.request.urlopen(req, timeout=30) as resposta:
        return json.load(resposta)


def consultar_previsao():
    return consultar_api(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": LATITUDE,
            "longitude": LONGITUDE,
            "timezone": "America/Sao_Paulo",
            "forecast_days": 5,
            "hourly": (
                "precipitation,"
                "precipitation_probability,"
                "weather_code"
            ),
            "daily": (
                "precipitation_sum,"
                "precipitation_probability_max,"
                "weather_code"
            ),
        },
    )


def enviar_telegram(mensagem):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    resposta = consultar_api(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {
            "chat_id": chat_id,
            "text": mensagem,
            "disable_web_page_preview": "true",
        },
    )

    if not resposta.get("ok"):
        raise RuntimeError("Telegram nao confirmou o envio")

    print("Mensagem enviada ao Telegram com sucesso.")


def ler_estado():
    if ARQUIVO_ESTADO.exists():
        with ARQUIVO_ESTADO.open(encoding="utf-8") as arquivo:
            return json.load(arquivo)

    return {}


def salvar_estado(estado):
    with ARQUIVO_ESTADO.open("w", encoding="utf-8") as arquivo:
        json.dump(estado, arquivo, indent=2, ensure_ascii=False)


def intensidade(mm):
    if mm < 2.5:
        return 1, "FRACA", "🌦️"
    if mm < 7.5:
        return 2, "MODERADA", "🌧️"
    return 3, "FORTE", "⛈️"


def recomendacoes(nivel):
    avisos = [
        "👕 Recolham as roupas do varal.",
        "🪟 Fechem as janelas e portas expostas.",
        "📦 Guardem objetos que podem molhar.",
    ]

    if nivel >= 2:
        avisos.append(
            "🚗 Evitem deixar objetos expostos na rua."
        )

    if nivel == 3:
        avisos.extend([
            "⚠️ Evitem areas com risco de alagamento.",
            "⚡ Se houver raios, procurem abrigo seguro.",
        ])

    return "\n".join(avisos)


def obter_horas(previsao):
    horas = []
    dados = previsao["hourly"]

    for indice, horario in enumerate(dados["time"]):
        horas.append({
            "inicio": datetime.fromisoformat(horario).replace(
                tzinfo=FUSO
            ),
            "mm": float(
                dados["precipitation"][indice] or 0
            ),
            "chance": int(
                dados["precipitation_probability"][indice] or 0
            ),
            "codigo": int(
                dados["weather_code"][indice] or 0
            ),
        })

    return horas


def resumo_diario(previsao):
    dados = previsao["daily"]
    horas = obter_horas(previsao)
    semana = [
        "segunda", "terca", "quarta", "quinta",
        "sexta", "sabado", "domingo"
    ]

    linhas = [
        "🌤️ BOM DIA, FAMILIA!",
        "",
        f"📍 {LOCAL}",
        "📅 PREVISAO DOS PROXIMOS 5 DIAS",
        "",
    ]

    dias_com_chuva = 0

    for indice, data in enumerate(dados["time"]):
        dia = datetime.fromisoformat(data)
        nome_dia = semana[dia.weekday()].capitalize()
        chance = int(
            dados["precipitation_probability_max"][indice] or 0
        )
        total = float(
            dados["precipitation_sum"][indice] or 0
        )

        # Maior intensidade horaria prevista naquele dia
        valores = [
            h["mm"] for h in horas
            if h["inicio"].date() == dia.date()
        ]
        pico = max(valores, default=0)

        if total >= 0.2 and chance >= CHANCE_MINIMA:
            _, nivel, icone = intensidade(pico)
            dias_com_chuva += 1
            texto = (
                f"{icone} {nome_dia} ({dia:%d/%m}): "
                f"chuva {nivel.lower()}, "
                f"chance maxima {chance}%, "
                f"acumulado {total:.1f} mm"
            )
        else:
            texto = (
                f"🌤️ {nome_dia} ({dia:%d/%m}): "
                f"sem chuva significativa prevista "
                f"(chance maxima {chance}%)"
            )

        linhas.append(texto)

    linhas.extend(["", "🏠 RECOMENDACAO:"])

    if dias_com_chuva:
        linhas.append(
            "Fiquem atentos ao varal e as janelas "
            "nos dias com previsao de chuva."
        )
    else:
        linhas.append(
            "Nao ha chuva significativa prevista "
            "pelos criterios do robo."
        )

    linhas.extend([
        "",
        "ℹ️ Previsao sujeita a mudancas.",
        "Fonte: Open-Meteo.",
    ])

    return "\n".join(linhas)


def verificar_resumo(previsao, estado, modo):
    momento = agora()
    hoje = momento.date().isoformat()

    deve_enviar = (
        modo == "resumo"
        or (
            7 <= momento.hour < 12
            and estado.get("ultimo_resumo") != hoje
        )
    )

    if not deve_enviar:
        return

    enviar_telegram(resumo_diario(previsao))
    estado["ultimo_resumo"] = hoje
    salvar_estado(estado)


def verificar_chuva(previsao, estado):
    momento = agora()
    horas = obter_horas(previsao)

    # A previsao horaria representa periodos de uma hora.
    # Procuramos os periodos que se sobrepoem aos
    # proximos 60 minutos, com margem de incerteza.
    limite = momento + timedelta(minutes=60)

    candidatos = []

    for hora in horas:
        fim_periodo = hora["inicio"]
        inicio_periodo = fim_periodo - timedelta(hours=1)

        if inicio_periodo > limite:
            continue

        if fim_periodo <= momento:
            continue

        if (
            hora["mm"] >= CHUVA_MINIMA
            and hora["chance"] >= CHANCE_MINIMA
        ):
            candidatos.append(hora)

    if not candidatos:
        print("Nenhuma chuva relevante prevista para a janela.")
        return

    # Escolher a maior intensidade na janela
    chuva = max(candidatos, key=lambda item: item["mm"])

    nivel, descricao, icone = intensidade(chuva["mm"])

    ultimo_alerta = estado.get("ultimo_alerta")
    ultimo_nivel = int(estado.get("ultimo_nivel", 0))

    pode_enviar = True

    if ultimo_alerta:
        ultimo = datetime.fromisoformat(ultimo_alerta)
        intervalo = momento - ultimo

        if intervalo < timedelta(
            hours=INTERVALO_ALERTAS_HORAS
        ):
            # Dentro do intervalo, somente um aumento
            # de intensidade justifica novo aviso.
            pode_enviar = nivel > ultimo_nivel

    if not pode_enviar:
        print("Alerta repetido evitado.")
        return

    if nivel == 3:
        titulo = "🚨 ATENCAO, FAMILIA! CHUVA FORTE!"
    elif nivel == 2:
        titulo = "⚠️ ATENCAO! CHUVA MODERADA!"
    else:
        titulo = "🌦️ AVISO DE POSSIVEL CHUVA!"

    mensagem = "\n".join([
        titulo,
        "",
        f"📍 {LOCAL}",
        f"{icone} Intensidade prevista: {descricao}",
        f"☔ Probabilidade horaria: {chuva['chance']}%",
        f"💧 Precipitacao estimada: {chuva['mm']:.1f} mm/h",
        "⏰ Possibilidade de chuva nos proximos 60 minutos.",
        "",
        "🏠 O QUE FAZER:",
        recomendacoes(nivel),
        "",
        "ℹ️ O inicio e a intensidade sao estimativas.",
        "A previsao horaria nao identifica o minuto exato.",
        "Fonte: Open-Meteo.",
    ])

    enviar_telegram(mensagem)

    estado["ultimo_alerta"] = momento.isoformat()
    estado["ultimo_nivel"] = nivel
    salvar_estado(estado)


def main():
    modo = sys.argv[1] if len(sys.argv) > 1 else "automatico"

    if modo == "teste":
        enviar_telegram(
            "✅ TESTE DO ALERTA DE CHUVA\n\n"
            "📍 Sao Miguel Paulista - SP\n\n"
            "O robo da familia esta funcionando! 🌧️\n\n"
            "Esta mensagem e apenas um teste."
        )
        return

    previsao = consultar_previsao()
    estado = ler_estado()

    if modo in ("automatico", "resumo"):
        verificar_resumo(previsao, estado, modo)

    if modo == "automatico":
        verificar_chuva(previsao, estado)


if __name__ == "__main__":
    main()
