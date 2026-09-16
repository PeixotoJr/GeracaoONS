"""
Dashboard interativo (Streamlit) — Geração de Usinas ONS.

Execução local:
    pip install -r requirements.txt
    streamlit run app.py
"""

import pandas as pd
import plotly.express as px
import streamlit as st

from ons_data import ONSDataProcessor, agregar, dimensoes_disponiveis, horas_no_intervalo, preparar_energia, totais_por_dimensao

st.set_page_config(page_title="ONS • Geração de Usinas", page_icon="⚡", layout="wide")


# ---------------------------------------------------------------------------
# Funções de carregamento (cacheadas)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False, ttl=60 * 60 * 6)
def baixar_periodo(start_year, start_month, end_year, end_month):
    processor = ONSDataProcessor()
    periods = processor.gerar_periodos(start_year, start_month, end_year, end_month)
    dfs = []
    progress = st.progress(0.0, text="Iniciando download...")
    for i, (year, month) in enumerate(periods):
        df = processor.download_month_data(year, month)
        if df is not None:
            dfs.append(df)
        progress.progress((i + 1) / len(periods), text=f"Baixado {year}-{month:02d} ({i + 1}/{len(periods)})")
    progress.empty()
    return processor.consolidate(dfs)


@st.cache_data(show_spinner=False)
def ler_arquivo(uploaded_file):
    if uploaded_file.name.endswith(".parquet"):
        df = pd.read_parquet(uploaded_file)
    else:
        df = pd.read_csv(uploaded_file, sep=None, engine="python")
    if "data_hora" not in df.columns and "din_instante" in df.columns:
        df["data_hora"] = pd.to_datetime(df["din_instante"])
    elif "data_hora" in df.columns:
        df["data_hora"] = pd.to_datetime(df["data_hora"])
    return df


@st.cache_data(show_spinner=False)
def to_csv_bytes(df):
    return df.to_csv(index=False).encode("utf-8")


# ---------------------------------------------------------------------------
# Barra lateral — fonte dos dados
# ---------------------------------------------------------------------------
st.sidebar.title("⚡ Fonte dos dados")
fonte = st.sidebar.radio("Como carregar os dados?", ["Baixar da ONS", "Carregar arquivo local"])

if fonte == "Baixar da ONS":
    col1, col2 = st.sidebar.columns(2)
    ano_ini = col1.number_input("Ano inicial", min_value=2000, max_value=2100, value=2024)
    mes_ini = col2.number_input("Mês inicial", min_value=1, max_value=12, value=1)
    col3, col4 = st.sidebar.columns(2)
    ano_fim = col3.number_input("Ano final", min_value=2000, max_value=2100, value=2024)
    mes_fim = col4.number_input("Mês final", min_value=1, max_value=12, value=12)

    if st.sidebar.button("Baixar dados", type="primary"):
        with st.spinner("Baixando dados da ONS — isso pode levar alguns minutos..."):
            df_baixado = baixar_periodo(int(ano_ini), int(mes_ini), int(ano_fim), int(mes_fim))
        if df_baixado.empty:
            st.sidebar.error("Nenhum dado encontrado para o período informado.")
        else:
            st.session_state["df_raw"] = df_baixado
            st.sidebar.success(f"{len(df_baixado):,} registros carregados.")
else:
    uploaded = st.sidebar.file_uploader("Arquivo consolidado (.parquet ou .csv)", type=["parquet", "csv"])
    if uploaded is not None:
        st.session_state["df_raw"] = ler_arquivo(uploaded)
        st.sidebar.success(f"{len(st.session_state['df_raw']):,} registros carregados.")

if "df_raw" not in st.session_state:
    st.title("⚡ Dashboard de Geração de Usinas — ONS")
    st.info(
        "Use a barra lateral para baixar os dados da ONS (por período) ou carregar um "
        "arquivo consolidado (.parquet/.csv) para começar."
    )
    st.stop()

df = st.session_state["df_raw"].copy()

if "data_hora" not in df.columns:
    st.error("O arquivo carregado não possui a coluna 'data_hora' (ou 'din_instante'). Verifique o arquivo.")
    st.stop()

df["data_hora"] = pd.to_datetime(df["data_hora"])
if "val_geracao" not in df.columns:
    st.error("O arquivo carregado não possui a coluna 'val_geracao'.")
    st.stop()

# ---------------------------------------------------------------------------
# Filtros
# ---------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.title("🔎 Filtros")

data_min, data_max = df["data_hora"].dt.date.min(), df["data_hora"].dt.date.max()
periodo_sel = st.sidebar.date_input("Período", value=(data_min, data_max), min_value=data_min, max_value=data_max)
if isinstance(periodo_sel, tuple) and len(periodo_sel) == 2:
    dt_ini, dt_fim = periodo_sel
else:
    dt_ini, dt_fim = data_min, data_max

df = df.loc[(df["data_hora"].dt.date >= dt_ini) & (df["data_hora"].dt.date <= dt_fim)]

dims = dimensoes_disponiveis(df)
for label, col in dims.items():
    valores = sorted(df[col].dropna().unique().tolist())
    if len(valores) > 1:
        selecionados = st.sidebar.multiselect(label, valores, default=[])
        if selecionados:
            df = df[df[col].isin(selecionados)]

if df.empty:
    st.warning("Nenhum registro encontrado para os filtros selecionados.")
    st.stop()

# ---------------------------------------------------------------------------
# Cabeçalho / KPIs
# ---------------------------------------------------------------------------
st.title("⚡ Dashboard de Geração de Usinas — ONS")
st.caption(f"Período: {dt_ini} a {dt_fim} • {len(df):,} registros horários")

# Regra: a energia (GWh) é calculada a partir do Δt real entre leituras de
# cada usina (Σ MW×Δt), robusta a lacunas/duplicidades; GWmed é derivado
# dividindo essa energia pelas horas de calendário do período.
horas_totais_filtro = horas_no_intervalo(dt_ini, dt_fim)
df_energia = preparar_energia(df)  # calcula delta_h/energia_mwh uma única vez

total_gwh = df_energia["energia_mwh"].sum() / 1000
media_gwmed = total_gwh / horas_totais_filtro

k1, k2, k3 = st.columns(3)
k1.metric("Energia total (GWh)", f"{total_gwh:,.2f}", help="Σ(MW × Δt real entre leituras) / 1000")
k2.metric("Potência média (GWmed)", f"{media_gwmed:,.3f}", help=f"GWh ÷ {horas_totais_filtro:,} horas do período")
k3.metric("Usinas distintas", f"{df['nom_usina'].nunique():,}" if "nom_usina" in df.columns else "—")

st.markdown("---")

# ---------------------------------------------------------------------------
# Abas: Diário / Mensal / Anual
# ---------------------------------------------------------------------------
abas = st.tabs(["📅 Diário", "🗓️ Mensal", "📆 Anual"])
granularidades = ["Diário", "Mensal", "Anual"]

for aba, granularidade in zip(abas, granularidades):
    with aba:
        c1, c2 = st.columns(2)
        metrica = c1.radio("Métrica", ["GWh", "GWmed"], horizontal=True, key=f"metrica_{granularidade}")
        opcoes_abertura = ["Nenhuma (total geral)"] + list(dims.keys())
        abertura_label = c2.selectbox("Abertura (dimensão)", opcoes_abertura, key=f"abertura_{granularidade}")
        coluna_dim = dims.get(abertura_label) if abertura_label != "Nenhuma (total geral)" else None

        agg = agregar(df_energia, granularidade, coluna_dim)

        st.caption(
            "GWh = Σ(MW × Δt real entre leituras da usina)/1000 • "
            f"GWmed = GWh ÷ horas do período ({granularidade.lower()}: 24h/dia, dias do mês×24, ou dias do ano×24)."
        )

        # --- Evolução temporal ---
        st.subheader(f"Evolução {granularidade.lower()} — {metrica}")
        if coluna_dim:
            top_n = st.slider("Mostrar top categorias (por total no período)", 3, 20, 8, key=f"topn_{granularidade}")
            top_categorias = agg.groupby(coluna_dim)[metrica].sum().sort_values(ascending=False).head(top_n).index
            agg_plot = agg[agg[coluna_dim].isin(top_categorias)].sort_values("periodo")
            fig = px.line(agg_plot, x="periodo", y=metrica, color=coluna_dim, markers=(granularidade != "Diário"))
        else:
            fig = px.line(agg.sort_values("periodo"), x="periodo", y=metrica, markers=(granularidade != "Diário"))
            fig.update_traces(fill="tozeroy")

        fig.update_layout(height=450, hovermode="x unified", legend_title_text=abertura_label)
        st.plotly_chart(fig, use_container_width=True)

        # --- Totais por abertura ---
        if coluna_dim:
            st.subheader(f"Total por {abertura_label}")
            tot = totais_por_dimensao(df_energia, coluna_dim, horas_totais_filtro)
            cbar, ctab = st.columns([1.3, 1])
            fig_bar = px.bar(
                tot.sort_values(metrica, ascending=True), x=metrica, y="categoria", orientation="h", text_auto=".2s"
            )
            fig_bar.update_layout(height=max(350, 28 * len(tot)), yaxis_title=abertura_label)
            cbar.plotly_chart(fig_bar, use_container_width=True)
            ctab.dataframe(
                tot.rename(columns={"categoria": abertura_label})[[abertura_label, "GWh", "GWmed", "n_registros"]],
                width="stretch",
                hide_index=True,
            )
            ctab.download_button(
                f"⬇️ Exportar totais por {abertura_label} (CSV)",
                data=to_csv_bytes(tot),
                file_name=f"totais_{abertura_label.lower().replace(' ', '_')}_{granularidade.lower()}.csv",
                mime="text/csv",
                key=f"dl_tot_{granularidade}",
            )

        # --- Tabela agregada + export ---
        with st.expander("Ver / exportar dados agregados desta aba"):
            st.dataframe(agg.sort_values("periodo"), width="stretch")
            #st.dataframe(agg.sort_values("periodo"), use_container_width=True, hide_index=True)
            st.download_button(
                "⬇️ Exportar dados agregados (CSV)",
                data=to_csv_bytes(agg),
                file_name=f"agregado_{granularidade.lower()}.csv",
                mime="text/csv",
                key=f"dl_agg_{granularidade}",
            )

st.markdown("---")
st.subheader("📦 Exportar dados consolidados (filtrados)")
c1, c2 = st.columns(2)
c1.download_button("⬇️ CSV completo (filtrado)", data=to_csv_bytes(df), file_name="geracao_usinas_filtrado.csv", mime="text/csv")
c2.download_button(
    "⬇️ Parquet completo (filtrado)",
    data=df.to_parquet(index=False),
    file_name="geracao_usinas_filtrado.parquet",
    mime="application/octet-stream",
)