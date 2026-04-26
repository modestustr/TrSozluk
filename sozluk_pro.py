import bz2
import os
import re
import sqlite3
import time
import unicodedata
import xml.etree.ElementTree as ET

import altair as alt
import pandas as pd
import requests
import streamlit as st


DB_FILE = "milli_sozluk_tr.db"
TEMP_BZ2_FILE = "trwiktionary-latest-pages-articles.xml.bz2"
DUMP_URL = "https://dumps.wikimedia.org/trwiktionary/latest/trwiktionary-latest-pages-articles.xml.bz2"
ESTIMATED_PAGES = 2_000_000
SEARCH_LIMIT = 20
INSERT_BATCH_SIZE = 5_000
ORIGIN_EXCLUDE_VALUES = ("Turkce", "Diger", "Türkçe", "Diğer")
TURKISH_ALPHABET = list("A B C Ç D E F G Ğ H I İ J K L M N O Ö P R S Ş T U Ü V Y Z".split())
TURKISH_ALPHABET_SET = set(TURKISH_ALPHABET)

LANGUAGE_NAMES = {
    "ar": "Arapça",
    "fa": "Farsça",
    "fr": "Fransızca",
    "en": "İngilizce",
    "el": "Yunanca",
    "it": "İtalyanca",
    "la": "Latince",
    "tr": "Türkçe",
}

LOCATION_KEYWORDS = [
    "ülke",
    "ülkeler",
    "köy",
    "ilçe",
    "belde",
    "belediyesi",
    "beldeler",
    "köyler",
    "ilçeler",
    "ili",
    "iline",
]

NAME_KEYWORDS = ["özel ad", "erkek ismi", "kız ismi", "soyadı"]

TURKISH_SECTION_RE = re.compile(
    r"==\s*(?:Turkce|T\u00fcrk\u00e7e)\s*==(.*?)(?=\n==[^=].*?==|\Z)",
    re.DOTALL | re.IGNORECASE,
)
DEFINITION_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
EXAMPLE_RE = re.compile(r"^#:\s*(.+)$", re.MULTILINE)
ORIGIN_RE = re.compile(r"\{\{\s*(?:[Kk]oken|[Kk]öken)\|([^|}]+)")
TEMPLATE_LANGUAGE_RE = re.compile(r"\|\s*dil\s*=\s*([a-zA-Z-]+)")

st.set_page_config(page_title="Milli Sözlük Gezgini", layout="wide")
st.markdown(
    """
    <style>
    .browse-entry {
        padding: 0.35rem 0 0.65rem 0;
        border-bottom: 1px solid rgba(120, 120, 120, 0.18);
    }
    .browse-word {
        font-size: 1rem;
        font-weight: 600;
        line-height: 1.35;
        margin-bottom: 0.2rem;
    }
    .browse-preview {
        font-size: 0.92rem;
        line-height: 1.55;
        color: rgba(49, 51, 63, 0.78);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def clean_wikitext(raw_text):
    if not raw_text:
        return ""

    text = str(raw_text)

    def replace_template(match):
        content = match.group(1).strip()
        parts = [part.strip() for part in content.split("|") if part.strip()]
        if not parts:
            return ""

        template_name = normalize_text(parts[0])
        named_args = {}
        positional_args = []

        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                named_args[normalize_text(key.strip())] = value.strip()
            else:
                positional_args.append(part)

        if template_name == "ozel ad":
            labels = [clean_wikitext(value) for value in positional_args if value]
            if labels:
                return f"Özel ad ({', '.join(labels)})"
            return "Özel ad"

        if template_name == "t":
            labels = [clean_wikitext(value) for value in positional_args if value]
            return ", ".join(labels)

        if template_name in {"koken", "koken"} and positional_args:
            return f"Köken: {clean_wikitext(positional_args[0])}"

        return ""

    text = re.sub(r"\{\{(.*?)\}\}", replace_template, text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"''+", "", text)
    text = re.sub(r"^\s*[:#\*]+\s*", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def normalize_text(text):
    if not text:
        return ""

    text = text.replace("\u0131", "i").replace("\u0130", "I")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return text.lower()


def turkish_upper(text):
    if not text:
        return ""
    mapping = str.maketrans({"i": "İ", "ı": "I"})
    return text.translate(mapping).upper()


def get_turkish_sort_key(value):
    if not value:
        return (999, "")

    first_char = turkish_upper(str(value).strip()[:1])
    try:
        alphabet_index = TURKISH_ALPHABET.index(first_char)
    except ValueError:
        alphabet_index = len(TURKISH_ALPHABET)
    return (alphabet_index, normalize_text(value))


NORMALIZED_LOCATION_KEYWORDS = [normalize_text(keyword) for keyword in LOCATION_KEYWORDS]
NORMALIZED_NAME_KEYWORDS = [normalize_text(keyword) for keyword in NAME_KEYWORDS]
LOCATION_PATTERNS = [
    re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)")
    for keyword in NORMALIZED_LOCATION_KEYWORDS
]
NAME_PATTERNS = [
    re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)")
    for keyword in NORMALIZED_NAME_KEYWORDS
]


def is_location(text):
    normalized = normalize_text(text)
    return any(pattern.search(normalized) for pattern in LOCATION_PATTERNS)


def is_name(text):
    normalized = normalize_text(text)
    return any(pattern.search(normalized) for pattern in NAME_PATTERNS)


def get_connection():
    return sqlite3.connect(DB_FILE)


def get_dictionary_columns(conn):
    cursor = conn.cursor()
    rows = cursor.execute("PRAGMA table_info(dictionary)").fetchall()
    return {row[1] for row in rows}


def initialize_database(conn):
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS dictionary")
    cursor.execute(
        """
        CREATE TABLE dictionary (
            word TEXT NOT NULL,
            browse_letter TEXT NOT NULL,
            word_normalized TEXT NOT NULL,
            meaning TEXT NOT NULL,
            origin TEXT NOT NULL,
            example TEXT DEFAULT ''
        )
        """
    )
    cursor.execute("CREATE INDEX idx_dictionary_word ON dictionary(word)")
    cursor.execute(
        "CREATE INDEX idx_dictionary_word_normalized ON dictionary(word_normalized)"
    )
    cursor.execute(
        "CREATE INDEX idx_dictionary_browse_letter_word ON dictionary(browse_letter, word_normalized, word)"
    )
    conn.commit()


def ensure_database_schema(conn):
    cursor = conn.cursor()
    tables = cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='dictionary'"
    ).fetchall()
    if not tables:
        return

    columns = get_dictionary_columns(conn)
    if "word_normalized" not in columns:
        cursor.execute(
            "ALTER TABLE dictionary ADD COLUMN word_normalized TEXT"
        )
        rows = cursor.execute("SELECT rowid, word FROM dictionary").fetchall()
        cursor.executemany(
            "UPDATE dictionary SET word_normalized = ? WHERE rowid = ?",
            [(normalize_text(word), rowid) for rowid, word in rows],
        )
        conn.commit()

    columns = get_dictionary_columns(conn)
    if "browse_letter" not in columns:
        cursor.execute(
            "ALTER TABLE dictionary ADD COLUMN browse_letter TEXT"
        )
        rows = cursor.execute("SELECT rowid, word FROM dictionary").fetchall()
        cursor.executemany(
            "UPDATE dictionary SET browse_letter = ? WHERE rowid = ?",
            [
                (turkish_upper(str(word).strip()[:1]) if word else "", rowid)
                for rowid, word in rows
            ],
        )
        conn.commit()

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dictionary_word ON dictionary(word)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dictionary_word_normalized ON dictionary(word_normalized)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_dictionary_browse_letter_word ON dictionary(browse_letter, word_normalized, word)"
    )
    conn.commit()


def extract_turkish_section(text):
    if not text:
        return ""

    match = TURKISH_SECTION_RE.search(text)
    if not match:
        return ""
    return match.group(1)


def extract_origin(section_text):
    match = ORIGIN_RE.search(section_text)
    if not match:
        return "Türkçe"

    raw_origin = match.group(1).strip().lower()
    return LANGUAGE_NAMES.get(raw_origin, raw_origin.title())


def extract_example(section_text):
    match = EXAMPLE_RE.search(section_text)
    return clean_wikitext(match.group(1)) if match else ""


def extract_meanings(section_text):
    definitions = DEFINITION_RE.findall(section_text)
    cleaned = [clean_wikitext(item) for item in definitions]
    return [item for item in cleaned if item]


def parse_page(title, text):
    if not title or not text or ":" in title:
        return None

    turkish_section = extract_turkish_section(text)
    if not turkish_section:
        return None

    meanings = extract_meanings(turkish_section)
    if not meanings:
        return None

    return {
        "word": title,
        "browse_letter": turkish_upper(title.strip()[:1]),
        "word_normalized": normalize_text(title),
        "meaning": " | ".join(meanings),
        "origin": extract_origin(turkish_section),
        "example": extract_example(turkish_section),
    }


def download_dump():
    st.info("Sözlük paketi indiriliyor...")

    with requests.get(DUMP_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0
        start_time = time.time()
        progress_bar = st.progress(0.0)
        status_text = st.empty()

        with open(TEMP_BZ2_FILE, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue

                downloaded += len(chunk)
                file.write(chunk)

                elapsed = time.time() - start_time
                speed = downloaded / elapsed if elapsed > 0 else 0
                eta = (total_size - downloaded) / speed if total_size and speed > 0 else 0

                if total_size:
                    progress_bar.progress(min(downloaded / total_size, 1.0))

                status_text.text(
                    f"İndiriliyor: %{(downloaded / total_size * 100) if total_size else 0:.1f} | "
                    f"Hız: {speed / 1024 / 1024:.2f} MB/s | Kalan: {int(eta)} sn"
                )


def flush_batch(cursor, batch_rows):
    if not batch_rows:
        return

    cursor.executemany(
        """
        INSERT INTO dictionary (word, browse_letter, word_normalized, meaning, origin, example)
        VALUES (:word, :browse_letter, :word_normalized, :meaning, :origin, :example)
        """,
        batch_rows,
    )


def parse_and_build_db():
    st.info("Veritabanı hazırlanıyor. Bu işlem zaman alabilir.")

    conn = get_connection()
    initialize_database(conn)
    cursor = conn.cursor()

    progress_bar = st.progress(0.0)
    status_text = st.empty()

    start_time = time.time()
    pages_processed = 0
    records_inserted = 0
    skipped_pages = 0
    batch_rows = []

    with bz2.BZ2File(TEMP_BZ2_FILE, "rb") as compressed_file:
        context = ET.iterparse(compressed_file, events=("end",))

        for _, elem in context:
            if not elem.tag.endswith("page"):
                continue

            pages_processed += 1

            title_node = elem.find(".//{*}title")
            text_node = elem.find(".//{*}text")
            title = title_node.text if title_node is not None else ""
            text = text_node.text if text_node is not None else ""

            parsed_row = parse_page(title, text)
            if parsed_row:
                batch_rows.append(parsed_row)
                records_inserted += 1
            else:
                skipped_pages += 1

            elem.clear()

            if len(batch_rows) >= INSERT_BATCH_SIZE:
                flush_batch(cursor, batch_rows)
                conn.commit()
                batch_rows = []

            if pages_processed % INSERT_BATCH_SIZE == 0:
                elapsed = time.time() - start_time
                speed = pages_processed / elapsed if elapsed > 0 else 0
                eta = (
                    (ESTIMATED_PAGES - pages_processed) / speed
                    if speed > 0
                    else 0
                )

                progress_bar.progress(min(pages_processed / ESTIMATED_PAGES, 1.0))
                status_text.text(
                    f"İşlenen: {pages_processed} sayfa | "
                    f"Kayıt: {records_inserted} | "
                    f"Atlanan: {skipped_pages} | "
                    f"Kalan: {int(eta)} sn"
                )

    flush_batch(cursor, batch_rows)
    conn.commit()
    conn.close()

    st.success(f"İşlem tamam. {records_inserted} madde kaydedildi.")


def load_origin_stats(conn):
    stats_df = pd.read_sql(
        """
        SELECT origin, COUNT(*) AS count
        FROM dictionary
        WHERE origin NOT IN ('Turkce', 'Diger', 'Türkçe', 'Diğer')
        GROUP BY origin
        ORDER BY count DESC
        LIMIT 12
        """,
        conn,
    )
    if stats_df.empty:
        return stats_df

    total = int(stats_df["count"].sum())
    stats_df["percentage"] = (stats_df["count"] / total * 100).round(1)
    return stats_df


def load_origin_overview(conn):
    overview_df = pd.read_sql(
        """
        SELECT
            COUNT(*) AS total_words,
            COUNT(DISTINCT origin) AS unique_origins
        FROM dictionary
        WHERE origin NOT IN ('Turkce', 'Diger', 'Türkçe', 'Diğer')
        """,
        conn,
    )
    row = overview_df.iloc[0]
    return {
        "total_words": int(row["total_words"] or 0),
        "unique_origins": int(row["unique_origins"] or 0),
    }


def classify_entry_type(meaning_text):
    meanings = str(meaning_text).split(" | ")
    has_general = False
    has_name = False
    has_location = False

    for meaning in meanings:
        cleaned_meaning = clean_wikitext(meaning)
        if not cleaned_meaning:
            continue

        location_match = is_location(meaning)
        name_match = is_name(meaning)

        if not location_match and not name_match:
            has_general = True
        elif name_match:
            has_name = True
        elif location_match:
            has_location = True

    if has_general:
        return "Genel kelime"
    if has_name:
        return "Özel ad"
    if has_location:
        return "Yerleşim"
    return "Diğer"


def load_entry_type_stats(conn):
    rows = conn.execute("SELECT meaning FROM dictionary").fetchall()
    counts = {
        "Genel kelime": 0,
        "Özel ad": 0,
        "Yerleşim": 0,
        "Diğer": 0,
    }

    for (meaning_text,) in rows:
        counts[classify_entry_type(meaning_text)] += 1

    stats_df = pd.DataFrame(
        [{"entry_type": key, "count": value} for key, value in counts.items() if value > 0]
    )
    if stats_df.empty:
        return stats_df

    total = int(stats_df["count"].sum())
    stats_df["percentage"] = (stats_df["count"] / total * 100).round(1)
    stats_df = stats_df.sort_values("count", ascending=False).reset_index(drop=True)
    return stats_df


def render_entry_type_analytics(conn):
    stats_df = load_entry_type_stats(conn)
    if stats_df.empty:
        st.caption("Kelime türü analitiği için yeterli veri yok.")
        return

    top_type = stats_df.iloc[0]
    metric_cols = st.columns(3)
    metric_cols[0].metric(
        "Toplam kayıt",
        f"{int(stats_df['count'].sum()):,}".replace(",", "."),
    )
    metric_cols[1].metric("Kategori", len(stats_df))
    metric_cols[2].metric(
        "En baskın tip",
        top_type["entry_type"],
        f"%{top_type['percentage']}",
    )

    col1, col2 = st.columns((1.3, 1))
    with col1:
        st.subheader("Kelime Tipi Oranı")
        st.altair_chart(
            alt.Chart(stats_df)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                x=alt.X("count:Q", title="Kayıt"),
                y=alt.Y("entry_type:N", sort="-x", title="Tip"),
                color=alt.Color("entry_type:N", legend=None),
                tooltip=["entry_type", "count", "percentage"],
            ),
            width="stretch",
        )

    with col2:
        st.subheader("Dağılım")
        st.altair_chart(
            alt.Chart(stats_df)
            .mark_arc(innerRadius=45)
            .encode(
                theta=alt.Theta("count:Q"),
                color=alt.Color("entry_type:N", legend=None),
                tooltip=["entry_type", "count", "percentage"],
            ),
            width="stretch",
        )
        st.dataframe(
            stats_df.rename(
                columns={
                    "entry_type": "Tip",
                    "count": "Kayıt",
                    "percentage": "Pay (%)",
                }
            ),
            hide_index=True,
            width="stretch",
        )


def count_meanings(meaning_text):
    meanings = str(meaning_text).split(" | ")
    cleaned = [clean_wikitext(meaning) for meaning in meanings]
    return len([meaning for meaning in cleaned if meaning])


def is_foreign_translation_meaning(meaning_text):
    text = str(meaning_text).strip()
    if "{{t|" not in text:
        return False

    match = TEMPLATE_LANGUAGE_RE.search(text)
    if not match:
        return False

    return normalize_text(match.group(1)) != "tr"


def count_non_location_meanings(meaning_text):
    meanings = str(meaning_text).split(" | ")
    count = 0

    for meaning in meanings:
        if is_foreign_translation_meaning(meaning):
            continue

        cleaned_meaning = clean_wikitext(meaning)
        if not cleaned_meaning:
            continue
        if not is_location(meaning):
            count += 1

    return count


def load_most_meanings_stats(conn):
    stats_df = pd.read_sql(
        """
        SELECT word, meaning, origin
        FROM dictionary
        """,
        conn,
    )
    if stats_df.empty:
        return stats_df

    stats_df["meaning_count"] = stats_df["meaning"].map(count_non_location_meanings)
    stats_df = stats_df[stats_df["meaning_count"] > 0].copy()
    if stats_df.empty:
        return stats_df

    stats_df = stats_df.sort_values(
        ["meaning_count", "word"],
        ascending=[False, True],
    ).head(15)
    return stats_df.reset_index(drop=True)


def render_most_meanings_analytics(conn):
    stats_df = load_most_meanings_stats(conn)
    if stats_df.empty:
        st.caption("Anlam yoğunluğu analitiği için yeterli veri yok.")
        return

    metric_cols = st.columns(3)
    metric_cols[0].metric("Listelenen kelime", len(stats_df))
    metric_cols[1].metric("Zirvedeki anlam", int(stats_df.iloc[0]["meaning_count"]))
    metric_cols[2].metric("İlk kelime", stats_df.iloc[0]["word"])

    chart_df = stats_df.rename(
        columns={
            "word": "Kelime",
            "origin": "Köken",
            "meaning_count": "Anlam sayısı",
        }
    )

    col1, col2 = st.columns((1.4, 1))
    with col1:
        st.subheader("En çok anlamı olan kelimeler")
        st.caption("Yerleşim anlamları bu analitiğe dahil edilmedi.")
        st.altair_chart(
            alt.Chart(chart_df)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                x=alt.X("Anlam sayısı:Q", title="Anlam sayısı"),
                y=alt.Y("Kelime:N", sort="-x", title="Kelime"),
                color=alt.Color("Köken:N", legend=None),
                tooltip=["Kelime", "Köken", "Anlam sayısı"],
            ),
            width="stretch",
        )

    with col2:
        st.subheader("İlk 15 tablo")
        st.dataframe(
            chart_df[["Kelime", "Köken", "Anlam sayısı"]],
            hide_index=True,
            width="stretch",
        )


def load_example_coverage_stats(conn, min_records=100):
    overview = pd.read_sql(
        """
        SELECT
            COUNT(*) AS total_records,
            SUM(CASE WHEN TRIM(COALESCE(example, '')) <> '' THEN 1 ELSE 0 END) AS with_examples
        FROM dictionary
        """,
        conn,
    ).iloc[0]

    by_origin = pd.read_sql(
        """
        SELECT
            origin,
            COUNT(*) AS total_records,
            SUM(CASE WHEN TRIM(COALESCE(example, '')) <> '' THEN 1 ELSE 0 END) AS with_examples
        FROM dictionary
        GROUP BY origin
        """,
        conn,
    )
    if not by_origin.empty:
        by_origin["coverage"] = (
            by_origin["with_examples"] / by_origin["total_records"] * 100
        ).round(1)
        overall_rate = (
            float(overview["with_examples"] or 0) / float(overview["total_records"] or 1)
        )
        prior_weight = max(int(min_records), 1)
        by_origin["weighted_coverage"] = (
            (
                by_origin["with_examples"] + overall_rate * prior_weight
            )
            / (by_origin["total_records"] + prior_weight)
            * 100
        ).round(1)
        by_origin = by_origin[by_origin["total_records"] >= min_records].copy()
        by_origin = by_origin.sort_values(
            ["weighted_coverage", "total_records"],
            ascending=[False, False],
        ).head(12)

    return {
        "total_records": int(overview["total_records"] or 0),
        "with_examples": int(overview["with_examples"] or 0),
        "by_origin": by_origin,
        "overall_rate": round(overall_rate * 100, 1) if not by_origin.empty else 0,
        "min_records": min_records,
    }


def render_example_coverage_analytics(conn):
    control_cols = st.columns((1, 1.5, 1.5))
    with control_cols[0]:
        min_records = st.selectbox(
            "Alt kayıt eşiği",
            options=[25, 50, 100, 250, 500],
            index=2,
        )

    stats = load_example_coverage_stats(conn, min_records=min_records)
    total_records = stats["total_records"]
    with_examples = stats["with_examples"]
    by_origin = stats["by_origin"]

    if total_records == 0:
        st.caption("Örnek cümle kapsaması için yeterli veri yok.")
        return

    coverage = round(with_examples / total_records * 100, 1) if total_records else 0
    metric_cols = st.columns(3)
    metric_cols[0].metric("Toplam kayıt", f"{total_records:,}".replace(",", "."))
    metric_cols[1].metric("Örnekli kayıt", f"{with_examples:,}".replace(",", "."))
    metric_cols[2].metric("Kapsama", f"%{coverage}")

    if by_origin.empty:
        st.caption("Seçili eşik için yeterli köken verisi yok.")
        return

    chart_df = by_origin.rename(
        columns={
            "origin": "Köken",
            "total_records": "Toplam kayıt",
            "with_examples": "Örnekli kayıt",
            "coverage": "Kapsama",
            "weighted_coverage": "Ağırlıklı kapsama",
        }
    )

    col1, col2 = st.columns((1.4, 1))
    with col1:
        st.subheader("Kökene göre örnek kapsaması")
        st.caption(
            f"Sıralama küçük örneklemleri dengelemek için ağırlıklı kapsama ile yapılıyor. "
            f"Genel ortalama: %{stats['overall_rate']}."
        )
        st.altair_chart(
            alt.Chart(chart_df)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                x=alt.X("Ağırlıklı kapsama:Q", title="Ağırlıklı kapsama (%)"),
                y=alt.Y("Köken:N", sort="-x", title="Köken"),
                color=alt.Color("Köken:N", legend=None),
                tooltip=["Köken", "Toplam kayıt", "Örnekli kayıt", "Kapsama", "Ağırlıklı kapsama"],
            ),
            width="stretch",
        )

    with col2:
        st.subheader("Kapsama tablosu")
        st.dataframe(
            chart_df[["Köken", "Toplam kayıt", "Örnekli kayıt", "Kapsama", "Ağırlıklı kapsama"]],
            hide_index=True,
            width="stretch",
        )


def load_initial_letter_stats(conn):
    rows = conn.execute("SELECT word FROM dictionary").fetchall()
    counts = {}

    for (word,) in rows:
        if not word:
            continue
        first_char = str(word).strip()[:1]
        if not first_char:
            continue
        first_char = turkish_upper(first_char)
        if first_char not in TURKISH_ALPHABET_SET:
            continue
        counts[first_char] = counts.get(first_char, 0) + 1

    stats_df = pd.DataFrame(
        [{"letter": letter, "count": count} for letter, count in counts.items()]
    )
    if stats_df.empty:
        return stats_df

    total = int(stats_df["count"].sum())
    stats_df["percentage"] = (stats_df["count"] / total * 100).round(1)
    return stats_df.sort_values(["count", "letter"], ascending=[False, True]).reset_index(
        drop=True
    )


def load_browse_letters(conn):
    rows = conn.execute(
        """
        SELECT browse_letter, COUNT(*) AS count
        FROM dictionary
        WHERE TRIM(COALESCE(browse_letter, '')) <> ''
        GROUP BY browse_letter
        """
    ).fetchall()

    letters = [
        {"letter": letter, "count": count}
        for letter, count in rows
        if letter in TURKISH_ALPHABET_SET
    ]
    letters.sort(key=lambda item: get_turkish_sort_key(item["letter"]))
    return letters


def load_browse_entries(conn, selected_letter, page_size, page_number):
    if not selected_letter:
        return pd.DataFrame(), 0
    if selected_letter not in TURKISH_ALPHABET_SET:
        return pd.DataFrame(), 0

    total_records = conn.execute(
        """
        SELECT COUNT(*)
        FROM dictionary
        WHERE browse_letter = ?
        """,
        (selected_letter,),
    ).fetchone()[0]

    if total_records == 0:
        return pd.DataFrame(), 0

    offset = (page_number - 1) * page_size
    rows_df = pd.read_sql(
        """
        SELECT word, meaning, origin
        FROM dictionary
        WHERE browse_letter = ?
        ORDER BY word_normalized, word
        LIMIT ? OFFSET ?
        """,
        conn,
        params=(selected_letter, page_size, offset),
    )
    if rows_df.empty:
        return rows_df, 0

    page_df = rows_df.copy()
    page_df["preview"] = page_df["meaning"].map(build_preview_text)
    return page_df, total_records


def render_initial_letter_analytics(conn):
    stats_df = load_initial_letter_stats(conn)
    if stats_df.empty:
        st.caption("Baş harf analitiği için yeterli veri yok.")
        return

    top_row = stats_df.iloc[0]
    top_three_share = round(stats_df.head(3)["percentage"].sum(), 1)
    metric_cols = st.columns(3)
    metric_cols[0].metric("İlk 3 harf payı", f"%{top_three_share}")
    metric_cols[1].metric("En yoğun harf", top_row["letter"])
    metric_cols[2].metric("Pay", f"%{top_row['percentage']}")

    top_letters = stats_df.head(15).rename(
        columns={
            "letter": "Harf",
            "count": "Kayıt",
            "percentage": "Pay (%)",
        }
    )

    col1, col2 = st.columns((1.4, 1))
    with col1:
        st.subheader("Baş harf dağılımı")
        st.altair_chart(
            alt.Chart(top_letters)
            .mark_bar(cornerRadiusEnd=4)
            .encode(
                x=alt.X("Kayıt:Q", title="Kayıt"),
                y=alt.Y("Harf:N", sort="-x", title="Harf"),
                color=alt.Color("Harf:N", legend=None),
                tooltip=["Harf", "Kayıt", "Pay (%)"],
            ),
            width="stretch",
        )

    with col2:
        st.subheader("İlk 15 harf")
        st.dataframe(
            top_letters,
            hide_index=True,
            width="stretch",
        )


def render_origin_analytics(conn):
    stats_df = load_origin_stats(conn)
    overview = load_origin_overview(conn)

    if stats_df.empty:
        st.caption("Köken analitiği için yeterli veri yok.")
        return

    top_origin = stats_df.iloc[0]
    metric_cols = st.columns(3)
    metric_cols[0].metric(
        "Kökenli kayıt",
        f"{overview['total_words']:,}".replace(",", "."),
    )
    metric_cols[1].metric("Benzersiz dil", overview["unique_origins"])
    metric_cols[2].metric(
        "En baskın köken",
        top_origin["origin"],
        f"%{top_origin['percentage']}",
    )

    chart_df = stats_df.copy()
    chart_df["label"] = chart_df["percentage"].map(lambda value: f"%{value}")

    col1, col2 = st.columns((1.6, 1))
    with col1:
        st.subheader("Köken Dağılımı")
        base_chart = alt.Chart(chart_df).encode(
            x=alt.X("count:Q", title="Kayıt"),
            y=alt.Y("origin:N", sort="-x", title="Dil"),
            color=alt.Color("origin:N", legend=None),
            tooltip=["origin", "count", "percentage"],
        )
        bar_chart = base_chart.mark_bar(cornerRadiusEnd=4)
        text_chart = base_chart.mark_text(
            align="left",
            baseline="middle",
            dx=6,
        ).encode(text="label:N")
        st.altair_chart(bar_chart + text_chart, width="stretch")

    with col2:
        st.subheader("İlk 5 Pay")
        pie_df = chart_df.head(5)
        st.altair_chart(
            alt.Chart(pie_df)
            .mark_arc(innerRadius=45)
            .encode(
                theta=alt.Theta("count:Q"),
                color=alt.Color("origin:N", legend=None),
                tooltip=["origin", "count", "percentage"],
            ),
            width="stretch",
        )

        st.dataframe(
            chart_df[["origin", "count", "percentage"]].rename(
                columns={
                    "origin": "Dil",
                    "count": "Kayıt",
                    "percentage": "Pay (%)",
                }
            ),
            hide_index=True,
            width='stretch',
        )


def render_sidebar(conn):
    st.sidebar.title("Yönetim ve Analiz")

    if st.sidebar.button("Veritabanını Sıfırla ve Yeniden Kur"):
        conn.close()
        os.remove(DB_FILE)
        st.rerun()

    try:
        stats_df = load_origin_stats(conn)
        if stats_df.empty:
            st.sidebar.caption("Köken istatistiği henüz oluşmadı.")
            return

        st.sidebar.subheader("Köken Özeti")
        for _, row in stats_df.head(5).iterrows():
            st.sidebar.write(
                f"{row['origin']}: {int(row['count']):,} kayıt (%{row['percentage']})".replace(",", ".")
            )
    except Exception as exc:
        st.sidebar.warning(f"İstatistikler yüklenemedi: {exc}")


def search_entries(conn, query_word):
    normalized_query = normalize_text(query_word)
    title_query = query_word[:1].upper() + query_word[1:] if query_word else query_word
    return pd.read_sql(
        """
        SELECT word, meaning, origin, example
        FROM dictionary
        WHERE word_normalized LIKE ?
        ORDER BY
            CASE
                WHEN word = ? THEN 0
                WHEN word = ? THEN 1
                ELSE 2
            END,
            LENGTH(word),
            word
        LIMIT ?
        """,
        conn,
        params=(f"{normalized_query}%", query_word, title_query, SEARCH_LIMIT),
    )


def render_search_results(results, show_locations):
    if results.empty:
        st.warning("Sonuç bulunamadı.")
        return

    for _, row in results.iterrows():
        meanings = row["meaning"].split(" | ")
        filtered_meanings = []

        for meaning in meanings:
            if is_foreign_translation_meaning(meaning):
                continue

            cleaned_meaning = clean_wikitext(meaning)
            if not cleaned_meaning:
                continue

            location_match = is_location(meaning)

            if (
                not location_match
                or (location_match and show_locations)
            ):
                filtered_meanings.append(cleaned_meaning)

        if not filtered_meanings:
            continue

        with st.container():
            st.markdown(f"### {row['word']}")
            st.caption(f"Köken: {row['origin']}")

            for index, meaning in enumerate(filtered_meanings, start=1):
                st.write(f"{index}. {meaning}")

            if row["example"]:
                st.info(f"Örnek: {clean_wikitext(row['example'])}")

            st.divider()


def build_preview_text(meaning_text, max_length=110):
    meanings = str(meaning_text).split(" | ")

    for meaning in meanings:
        if is_foreign_translation_meaning(meaning):
            continue

        cleaned_meaning = clean_wikitext(meaning)
        if not cleaned_meaning:
            continue

        preview = cleaned_meaning.strip()
        if len(preview) <= max_length:
            return preview
        return preview[: max_length - 1].rstrip() + "…"

    return ""


def render_search_tab(conn):
    col1, col2 = st.columns(2)
    with col1:
        show_locations = st.checkbox(
            "Yerleşim yerlerini göster",
            value=False,
            help="Köy, ilçe, belde gibi tanımları sonuçlara dahil eder.",
        )
    with col2:
        st.caption("Özel isimler varsayılan olarak gösterilir.")

    query_word = st.text_input(
        "Kelime ara",
        placeholder="Örn: uysal, kalem, vapur",
    ).strip()

    if not query_word:
        st.info("Aramaya başlamak için bir kelime yazın.")
        return

    results = search_entries(conn, query_word)
    render_search_results(results, show_locations)


def render_browse_tab(conn):
    st.caption("Basılı sözlük hissi için alfabetik fihrist.")

    letters = load_browse_letters(conn)
    if not letters:
        st.info("Fihrist için yeterli kayıt yok.")
        return

    letter_options = [item["letter"] for item in letters]
    counts_by_letter = {item["letter"]: item["count"] for item in letters}

    controls_col1, controls_col2, controls_col3 = st.columns((1, 1, 1.2))
    with controls_col1:
        selected_letter = st.selectbox(
            "Harf",
            options=letter_options,
            index=0,
        )
    with controls_col2:
        page_size = st.selectbox(
            "Sayfa boyu",
            options=[30, 60, 90],
            index=1,
        )

    total_for_letter = counts_by_letter[selected_letter]
    total_pages = max((total_for_letter - 1) // page_size + 1, 1)

    with controls_col3:
        page_number = st.number_input(
            "Sayfa",
            min_value=1,
            max_value=total_pages,
            value=1,
            step=1,
        )

    browse_df, total_records = load_browse_entries(
        conn,
        selected_letter,
        page_size,
        int(page_number),
    )

    if browse_df.empty:
        st.info("Bu harfte gösterilecek kayıt yok.")
        return

    st.markdown(
        f"**{selected_letter} harfi** altında **{total_records:,}** başlık bulunuyor. "
        f"Fihristin **{int(page_number)} / {total_pages}** sayfası gösteriliyor.".replace(",", ".")
    )

    preview_cols = st.columns(2)
    for index, row in browse_df.reset_index(drop=True).iterrows():
        target_col = preview_cols[index % 2]
        with target_col:
            preview_text = row["preview"] if row["preview"] else "Önizleme bulunamadı."
            st.markdown(
                f"""
                <div class="browse-entry">
                    <div class="browse-word">{row['word']}</div>
                    <div class="browse-preview">{preview_text}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_analytics_tab(conn):
    st.subheader("Köken dağılımı")
    render_origin_analytics(conn)
    st.divider()
    st.subheader("Özel ad / yerleşim / genel kelime oranı")
    render_entry_type_analytics(conn)
    st.divider()
    st.subheader("En çok anlamı olan kelimeler")
    render_most_meanings_analytics(conn)
    st.divider()
    st.subheader("Örnek cümle kapsama oranı")
    render_example_coverage_analytics(conn)
    st.divider()
    st.subheader("Baş harf dağılımı")
    render_initial_letter_analytics(conn)


def render_main_ui(conn):
    st.title("Milli Sözlük Gezgini")

    search_tab, browse_tab, analytics_tab = st.tabs(["Kelime Ara", "Fihrist", "Analitikler"])

    with search_tab:
        render_search_tab(conn)

    with browse_tab:
        render_browse_tab(conn)

    with analytics_tab:
        render_analytics_tab(conn)


def render_setup_ui():
    st.title("Sözlük Kurulumu")
    st.write("Veritabanı oluşturulacak. Yerel dump dosyası yoksa indirilecek.")

    if st.button("Kurulumu Başlat"):
        try:
            if not os.path.exists(TEMP_BZ2_FILE):
                download_dump()
            parse_and_build_db()
            st.rerun()
        except requests.RequestException as exc:
            st.error(f"İndirme hatası: {exc}")
        except Exception as exc:
            st.error(f"Kurulum sırasında hata oluştu: {exc}")


def main():
    if not os.path.exists(DB_FILE):
        render_setup_ui()
        return

    conn = get_connection()
    try:
        ensure_database_schema(conn)
        render_sidebar(conn)
        render_main_ui(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
