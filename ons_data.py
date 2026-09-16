"""
Módulo de acesso e processamento dos dados de Geração de Usinas (ONS).

Isola a lógica de download/consolidação (adaptada do script original) para
ser reutilizada pelo dashboard Streamlit em app.py.
"""

import calendar
import io
import logging

import pandas as pd
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Dimensões de "abertura" tipicamente presentes no dataset GERACAO_USINA-2 da
# ONS. Nem todo arquivo traz todas as colunas — o dashboard verifica
# dinamicamente quais existem no DataFrame carregado.
DIMENSOES_ABERTURA = {
    "Subsistema": "nom_subsistema",
    "Estado": "nom_estado",
    "Tipo de Usina": "nom_tipousina",
    "Tipo de Combustível": "nom_tipocombustivel",
    "Usina": "nom_usina",
}

MW_PARA_GW = 1000.0


class ONSDataProcessor:
    """Baixa e consolida os arquivos parquet mensais publicados pela ONS."""

    def __init__(self, base_url="https://ons-aws-prod-opendata.s3.amazonaws.com/dataset/geracao_usina_2_ho"):
        self.base_url = base_url.rstrip("/")

    def download_month_data(self, year, month):
        """Baixa e prepara os dados de um único mês. Retorna None em caso de erro/404."""
        month_str = str(month).zfill(2)
        url = f"{self.base_url}/GERACAO_USINA-2_{year}_{month_str}.parquet"
        try:
            logging.info(f"Baixando dados: {year}-{month_str}")
            response = requests.get(url, timeout=180)
            response.raise_for_status()
            df = pd.read_parquet(io.BytesIO(response.content))

            if "val_geracao" in df.columns:
                df["val_geracao"] = pd.to_numeric(df["val_geracao"], errors="coerce")

            df["ano"] = year
            df["mes"] = month
            df["data_hora"] = pd.to_datetime(df["din_instante"])
            df["data"] = df["data_hora"].dt.date
            df["hora"] = df["data_hora"].dt.hour

            logging.info(f"OK {year}-{month_str} | Registros: {len(df):,}")
            return df
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 404:
                logging.warning(f"Arquivo não encontrado para {year}-{month_str}")
            else:
                logging.error(f"Erro HTTP ao baixar {year}-{month_str}: {e}")
            return None
        except Exception as e:
            logging.error(f"Erro ao processar {year}-{month_str}: {e}")
            return None

    @staticmethod
    def gerar_periodos(start_year, start_month, end_year, end_month):
        periods = []
        year, month = start_year, start_month
        while (year < end_year) or (year == end_year and month <= end_month):
            periods.append((year, month))
            month += 1
            if month > 12:
                month = 1
                year += 1
        return periods

    def download_range(self, start_year, start_month, end_year, end_month, progress_callback=None):
        """
        Baixa sequencialmente os períodos solicitados.
        `progress_callback(indice, total, ano, mes)` é chamado a cada período
        processado — útil para atualizar uma barra de progresso do Streamlit.
        """
        periods = self.gerar_periodos(start_year, start_month, end_year, end_month)
        resultados = []
        for i, (year, month) in enumerate(periods):
            df = self.download_month_data(year, month)
            if df is not None:
                resultados.append(df)
            if progress_callback:
                progress_callback(i + 1, len(periods), year, month)
        return resultados

    @staticmethod
    def consolidate(dataframes):
        if not dataframes:
            return pd.DataFrame()
        df = pd.concat(dataframes, ignore_index=True)
        if "val_geracao" in df.columns:
            df["val_geracao"] = pd.to_numeric(df["val_geracao"], errors="coerce")
        if "data_hora" in df.columns:
            df = df.sort_values("data_hora")
        return df


def dimensoes_disponiveis(df):
    """Retorna o subconjunto de DIMENSOES_ABERTURA presente no DataFrame."""
    return {label: col for label, col in DIMENSOES_ABERTURA.items() if col in df.columns}


def horas_no_periodo(periodo, granularidade):
    """
    Número de horas do intervalo de calendário correspondente a `periodo`,
    de acordo com a granularidade:

    - Diário: 24
    - Mensal: dias do mês × 24
    - Anual : dias do ano × 24 (365 ou 366, respeitando anos bissextos)
    """
    if granularidade == "Diário":
        return 24
    elif granularidade == "Mensal":
        ts = pd.Timestamp(periodo)
        dias_no_mes = calendar.monthrange(ts.year, ts.month)[1]
        return dias_no_mes * 24
    else:  # Anual
        ano = int(periodo)
        dias_no_ano = 366 if calendar.isleap(ano) else 365
        return dias_no_ano * 24


def horas_no_intervalo(dt_ini, dt_fim):
    """Número de horas entre duas datas (inclusive), assumindo cobertura contínua."""
    dt_ini = pd.Timestamp(dt_ini).normalize()
    dt_fim = pd.Timestamp(dt_fim).normalize()
    dias = (dt_fim - dt_ini).days + 1
    return dias * 24


# Colunas candidatas a identificar uma série temporal única (uma usina), em
# ordem de preferência. Necessário porque a base tem várias usinas no mesmo
# arquivo — o Δt entre leituras só pode ser calculado dentro da série de
# CADA usina, nunca na tabela inteira misturada.
ENTIDADE_COLS_PRIORIDADE = ["id_ons", "ceg", "nom_usina"]


def _coluna_entidade(df):
    for col in ENTIDADE_COLS_PRIORIDADE:
        if col in df.columns:
            return col
    return None


def preparar_energia(df):
    """
    Calcula a energia (MWh) de cada registro de forma robusta a intervalos
    irregulares, lacunas e duplicidades — em vez de assumir 1h por registro:

        Δt_i = tempo até a PRÓXIMA leitura da MESMA usina (em horas)
        E_i  = MW_i × Δt_i

    O Δt é calculado por usina (agrupando por id_ons/ceg/nom_usina, o que
    houver disponível), nunca na tabela inteira. Para o último registro de
    cada usina (sem leitura seguinte) ou intervalos ausentes, usa-se a
    mediana dos intervalos daquela mesma usina como estimativa; na falta
    dela, a mediana global; em último caso, 1h.

    Retorna o DataFrame com as colunas adicionais `delta_h` e `energia_mwh`.
    """
    if "energia_mwh" in df.columns and "delta_h" in df.columns:
        return df  # já preparado

    df = df.copy()
    entidade_col = _coluna_entidade(df)

    if entidade_col:
        df = df.sort_values([entidade_col, "data_hora"])
        prox_ts = df.groupby(entidade_col)["data_hora"].shift(-1)
    else:
        df = df.sort_values("data_hora")
        prox_ts = df["data_hora"].shift(-1)

    delta_h = (prox_ts - df["data_hora"]).dt.total_seconds() / 3600

    if entidade_col:
        mediana_entidade = delta_h.groupby(df[entidade_col]).transform("median")
    else:
        mediana_entidade = pd.Series(delta_h.median(), index=df.index)
    mediana_global = delta_h.median()

    delta_h = delta_h.fillna(mediana_entidade).fillna(mediana_global).fillna(1.0)
    delta_h = delta_h.where(delta_h > 0, 1.0)  # protege contra timestamps duplicados/fora de ordem

    df["delta_h"] = delta_h
    df["energia_mwh"] = df["val_geracao"] * df["delta_h"]
    return df


def agregar(df, granularidade, coluna_dimensao=None):
    """
    Agrega por período (Diário/Mensal/Anual) e, opcionalmente, por uma
    dimensão de abertura.

    Regra de conversão (energia é a grandeza "base", calculada da série real;
    GWmed é derivado dela dividindo pela duração do período):

        E_i (MWh) = MW_i × Δt_i             (Δt real entre leituras da usina)
        GWh       = Σ E_i / 1000
        GWmed     = GWh / horas_do_período   (24h/dia, dias_do_mês×24, dias_do_ano×24)

    GWh é robusto a lacunas, duplicidades e intervalos irregulares (usa o Δt
    real de cada usina). GWmed representa a potência média equivalente do
    grupo ao longo de todo o período de calendário (não da soma de horas de
    cada usina individualmente), que é a definição física correta de
    potência média para um conjunto de usinas.
    """
    df = preparar_energia(df)
    entidade_col = _coluna_entidade(df)

    if granularidade == "Diário":
        df["periodo"] = df["data_hora"].dt.date
    elif granularidade == "Mensal":
        df["periodo"] = df["data_hora"].dt.to_period("M").dt.to_timestamp()
    else:  # Anual
        df["periodo"] = df["data_hora"].dt.year

    group_cols = ["periodo"] + ([coluna_dimensao] if coluna_dimensao else [])

    agg_kwargs = {
        "energia_mwh": ("energia_mwh", "sum"),
        "n_registros": ("val_geracao", "count"),
        "horas_cobertas": ("delta_h", "sum"),
    }
    if entidade_col:
        agg_kwargs["n_usinas"] = (entidade_col, "nunique")

    agg = df.groupby(group_cols, dropna=False).agg(**agg_kwargs).reset_index()

    agg["GWh"] = agg["energia_mwh"] / MW_PARA_GW
    agg["horas_periodo"] = agg["periodo"].apply(lambda p: horas_no_periodo(p, granularidade))
    agg["GWmed"] = agg["GWh"] / agg["horas_periodo"]

    # Diagnóstico de cobertura: horas efetivamente cobertas pelos dados vs.
    # horas esperadas (horas do período × nº de usinas do grupo).
    horas_esperadas = agg["horas_periodo"] * agg["n_usinas"] if "n_usinas" in agg.columns else agg["horas_periodo"]
    agg["cobertura_%"] = (agg["horas_cobertas"] / horas_esperadas * 100).round(1)

    return agg.drop(columns=["energia_mwh", "horas_cobertas"])


def totais_por_dimensao(df, coluna_dimensao, horas_totais):
    """
    Total por categoria de uma dimensão, no período filtrado.

    Mesma regra de `agregar`: GWh = Σ(MW × Δt_real)/1000 e
    GWmed = GWh / horas_totais, onde `horas_totais` é o número de horas do
    intervalo de datas selecionado no filtro (ver `horas_no_intervalo`).
    """
    df = preparar_energia(df)

    agg = df.groupby(coluna_dimensao, dropna=False).agg(
        energia_mwh=("energia_mwh", "sum"),
        n_registros=("val_geracao", "count"),
    ).reset_index()
    agg = agg.rename(columns={coluna_dimensao: "categoria"})

    agg["GWh"] = agg["energia_mwh"] / MW_PARA_GW
    agg["GWmed"] = agg["GWh"] / horas_totais

    agg = agg.drop(columns=["energia_mwh"])
    return agg.sort_values("GWh", ascending=False).reset_index(drop=True)