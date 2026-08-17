"""
LastPage.py

Extract and Verify Last Page Objects:
  1. Address (common for all languages)
  2. Disclaimer Text & Copyright / Trademark Text (language dependent)
  3. Last Page Footer Line Metadata Detection & Verification:
     - Document Number (e.g. 882597)
     - Version (e.g. 5.0)
     - Language Code (e.g. sv-SE, en-US) - Verified against Excel language list
     - Revision / Release Date Code (e.g. 2026-04) - Verified against English copy date
     - Manual Type Code (e.g. IOM) - Mapped & verified against English copy manual type
     - Product Name (e.g. Start 350)
"""

import os
import re
import pymupdf


# =========================================================
# ADDRESS (Common for all languages, not language dependent)
# =========================================================

address = """
Xylem Water Solutions Global
Services AB 556782-9253
361 80 Emmaboda
Sweden
Tel: +46-471-24 70 00
Fax: +46-471-24 74 01
https://tpi.xylem.com
"""


# =========================================================
# SUPPORTED EXCEL LANGUAGE CODES (Hyphen & Underscore normalized)
# =========================================================

EXCEL_LANGUAGE_CODES = {
    "AR_EG", "BG_BG", "HR_HR", "CS_CZ", "DA_DK", "NL_NL",
    "ET_EE", "FI_FI", "FR_CA", "FR_FR", "DE_DE", "EL_GR",
    "HE_IL", "HU_HU", "IS_IS", "GA_IE", "IT_IT", "KO_KR",
    "LV_LV", "LT_LT", "MT_MT", "NO_NO", "PL_PL", "PT_BR",
    "PT_PT", "RO_RO", "RU_RU", "SR_SP", "ZH_CN", "SK_SK",
    "SL_SI", "ES_LA", "ES_ES", "SV_SE", "TR_TR", "UK_UA",
    "EN_US", "EN_GB", "EN", "DA", "DE", "EL", "ES", "FI",
    "FR", "IT", "NL", "NO", "PT", "SV"
}


# =========================================================
# MANUAL TYPE TO CODE MAPPING & REVERSE MAPPING
# =========================================================

MANUAL_TYPE_CODE_MAP = {
    "Installation, Operation, and Maintenance": "IOM",
    "Datasheet/Cutsheet": "DS",
    "Quick Start Guide": "QSG",
    "User Interface": "UI",
    "Product specification": "PS",
    "Pump curve": "PC",
    "Global Product List": "GPL",
    "Product Sustainability Report": "PSR",
    "Technical Specification": "TS",
    "Basic Repair Kit/Spares Leaflet": "BRK",
    "Checklist": "CL",
    "General Safety Information": "GSI",
    "Software strings": "SS",
    "Installation guide": "IG",
    "Mounting Instructions": "MI",
    "Service and Repair manual": "SR",
    "User Guide": "UG",
    "Acrolinx": "Acrolinx",
    "Others": "Others",
}

# Reverse mapping: Code -> Full Manual Type Name
CODE_TO_MANUAL_TYPE_MAP = {v.upper(): k for k, v in MANUAL_TYPE_CODE_MAP.items()}


# =========================================================
# DISCLAIMER TEXT BY LANGUAGE CODE
# =========================================================

disclaimer_text = {
    "EN": """Visit our website for the latest version of this document and more information.
The original instruction is in English. All non-English instructions are translations of
the original instruction.""",

    "DA": """Besøg vores hjemmeside for den seneste version af dette dokument og for yderligere
oplysninger.
De originale instruktionerne er på engelsk. Alle ikke-engelske instruktioner er
oversættelser af den originale instruktion.""",

    "DE": """Auf unserer Website finden Sie die aktuellste Version dieses Dokuments sowie
weitere Informationen.
Die Original-Betriebsanleitung ist auf Englisch abgefasst. Alle in anderen Sprachen
abgefassten Betriebsanleitungen sind Übersetzungen der Original-Betriebsanleitung.""",

    "EL": """Για να βρείτε την πιο πρόσφατη έκδοση του εγγράφου και να μάθετε περισσότερες
πληροφορίες, επισκεφτείτε τον ιστότοπό μας.
Η αρχική οδηγία είναι στα Αγγλικά. Όλες οι μη Αγγλικές οδηγίες αποτελούν
μεταφράσεις της αρχικής οδηγίας.""",

    "ES": """Visite nuestra página web para ver la última versión de este documento y obtener
más información.
Las instrucciones originales están en inglés. Todas las instrucciones que no estén en
inglés son traducciones de las originales.""",

    "FI": """Sivustoltamme löydät tämän asiakirjan uusimman version ja lisätietoja.
Alkuperäisohje on englanninkielinen. Kaikki muunkieliset ohjeet ovat alkuperäisen
ohjeen käännöksiä.""",

    "FR": """Pour obtenir un complément d'informations et consulter la version la plus récente de
ce document, rendez-vous sur notre site web.
Les instructions originales ont été rédigées en anglais. Toutes les instructions dans
des langues autres que l'anglais sont des traductions des instructions originales.""",

    "IT": """Visitare il nostro sito web le l'ultima versione di questo documento e per ulteriori
informazioni.
Le istruzioni originali sono in inglese. Tutte le istruzioni non in inglese sono la
traduzione delle istruzioni originali.""",

    "NL": """Ga naar onze website voor de meest recente versie van dit document en voor meer
informatie.
Alle instructies komen oorspronkelijk uit het Engels. Alle instructies niet in het Engels
zijn vertalingen van de orginele instructie.""",

    "NO": """Besøk vårt nettsted for å finne den nyeste versjonen av dette dokumentet og mer
informasjon.
De opprinnelige instruksjonene er på engelsk. Instruksjoner på andre språk er en
oversettelse fra opprinnelige instruksjonene.""",

    "PT": """Aceda ao nosso site para a versão mais recente deste documento e mais
informações.
As instruções originais estão disponíveis em inglês. Todas as instruções que não
estão em Inglês são traduções das instruções originais.""",

    "SV": """Gå till vår webbplats för den senaste versionen av det här dokumentet och för mer
information.
Originalanvisningarna är på engelska. Alla anvisningar som inte är på engelska har
översatts från originalet."""
}


# =========================================================
# COPYRIGHT / TRADEMARK TEXT BY LANGUAGE CODE
# =========================================================

copy_rights = {
    "EN": """©2012 – 2026 Xylem Inc. Flygt is a trademark of Xylem Inc. or one of its subsidiaries.
All other trademarks or registered trademarks are property of their respective owners""",

    "DA": """©2012 – 2026 Xylem Inc. Flygt er et varemærke tilhørende Xylem Inc. eller et af deres
datterselskaber. Alle andre varemærker eller registrerede varemærker tilhører deres
respektive ejere.""",

    "DE": """©2012 – 2026 Xylem Inc. Flygt ist eine Marke von Xylem Inc. oder einem seiner
Tochterunternehmen. Alle anderen Marken und eingetragenen Marken sind Eigentum
ihrer jeweiligen Inhaber.""",

    "EL": """©2012 – 2026 Xylem Inc. Η ονομασία Flygt είναι εμπορικό σήμα της Xylem Inc. ή
των θυγατρικών της. Όλα τα υπόλοιπα εμπορικά σήματα ή σήματα κατατεθέντα είναι
ιδιοκτησία των αντίστοιχων κατόχων τους.""",

    "ES": """©2012 – 2026 Xylem Inc. Flygt es una marca comercial de Xylem Inc. o de una de
sus filiales. El resto de las marcas comerciales o las marcas comerciales registradas
pertenecen a sus respectivos propietarios.""",

    "FI": """©2012 – 2026 Xylem Inc. Flygt onXylem Inc.-yhtiön tai sen tytäryhtiön tavaramerkki.
Kaikki muut tavaramerkit tai rekisteröidy tavaramerkit ovat omistajiensa omaisuutta.""",

    "FR": """©2012 – 2026 Xylem Inc. Flygt est une marque de Xylem Inc. ou de l’une de ses
filiales. Toutes les autres marques ou marques déposées sont la propriété de leurs
propriétaires respectifs.""",

    "IT": """©2012 – 2026 Xylem Inc. Flygt è un marchio registrato di Xylem Inc. o una delle
sue affiliate. Tutti gli altri marchi commerciali o registrati appartengono ai rispettivi
proprietari.""",

    "NL": """© 2012 – 2026 Xylem Inc. Flygt is een merk van Xylem Inc. of een van diens
dochterondernemingen. Alle andere handelsmerken of geregistreerde handelsmerken
zijn eigendom van hun betreffende eigenaars.""",

    "NO": """© 2012 – 2026 Xylem Inc. Flygt er et varemerke som tilhører Xylem eller ett av
selskapets datterselskaper. Alle andre varemerker og registrerte varemerker tilhører
de respektive eierne.""",

    "PT": """© 2012 – 2026 Xylem Inc. Flygt é uma marca comercial da Xylem Inc. ou de uma
das suas subsidiárias. Todas as outra marcas comerciais ou marcas comerciais
registadas são propriedade dos respetivos detentores.""",

    "SV": """© 2012 – 2026 Xylem Inc. Flygt är ett varumärke som tillhör Xylem Inc. eller ett
av dess dotterbolag. Alla andra varumärken och registrerade varumärken tillhör sina
respektive ägare.""",
}


# =========================================================
# HELPER FUNCTIONS TO RETRIEVE BY LANGUAGE CODE
# =========================================================

def get_disclaimer(lang_code="EN"):
    """
    Retrieve disclaimer text for the specified language code.
    Fallback to English ('EN') if language code is not found.
    """
    code = str(lang_code).upper() if lang_code else "EN"

    if "_" in code or "-" in code:
        code = re.split(r"[-_]", code)[0]

    if isinstance(disclaimer_text, dict):
        return disclaimer_text.get(code, disclaimer_text.get("EN"))
    return disclaimer_text


def get_copyright(lang_code="EN"):
    """
    Retrieve copyright text for the specified language code.
    Fallback to English ('EN') if language code is not found.
    """
    code = str(lang_code).upper() if lang_code else "EN"

    if "_" in code or "-" in code:
        code = re.split(r"[-_]", code)[0]

    if isinstance(copy_rights, dict):
        return copy_rights.get(code, copy_rights.get("EN"))
    return copy_rights


# =========================================================
# NORMALIZE TEXT
# =========================================================

def normalize_text(text):
    if not text:
        return ""
    text = text.lower()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("© ", "©")
    text = text.replace("ĳ", "ij")  # Normalize Dutch IJ ligature
    text = re.sub(r"\s+", " ", text)
    text = text.replace(
        "[https://tpi.xylem.com](https://tpi.xylem.com)",
        "https://tpi.xylem.com"
    )
    text = text.replace(
        "[https://tpi.xylem.com]",
        "https://tpi.xylem.com"
    )
    return text.strip()


# =========================================================
# FOOTER LINE PARSER (ORDER-INDEPENDENT)
# =========================================================

def parse_footer_line(line_text):
    """
    Parses the last line metadata string (e.g. '882597_5.0_sv-SE_2026-04_IOM_Start 350')
    in an order-independent fashion.

    Extracts:
      - doc_number        : e.g. '882597'
      - version           : e.g. '5.0'
      - language_code     : e.g. 'sv-SE'
      - date_code         : e.g. '2026-04'
      - manual_type_code  : e.g. 'IOM'
      - product_name      : e.g. 'Start 350'
    """
    if not line_text:
        return {
            "raw_line": "",
            "doc_number": None,
            "version": None,
            "language_code": None,
            "date_code": None,
            "manual_type_code": None,
            "product_name": None,
            "is_valid_footer": False
        }

    clean_line = line_text.strip()

    doc_number = None
    version = None
    language_code = None
    date_code = None
    manual_type_code = None

    # 1. Revision Date (e.g. 2026-04 or 2026_04)
    m = re.search(r"(?:^|[\s_.-])(\d{4}[-_]\d{2})(?:$|[\s_.-])", clean_line)
    if m:
        date_code = m.group(1)

    # 2. Version (e.g. 5.0)
    m = re.search(r"(?:^|[\s_.-])(\d+\.\d+)(?:$|[\s_.-])", clean_line)
    if m:
        version = m.group(1)

    # 3. Document Number (e.g. 882597 or 894387)
    m = re.search(r"(?:^|[\s_.-])(\d{5,8})(?:$|[\s_.-])", clean_line)
    if m:
        doc_number = m.group(1)

    # 4. Language Code (e.g. sv-SE, sv_SE, en-US)
    m = re.search(r"(?:^|[\s_.-])([a-zA-Z]{2}[-_][a-zA-Z]{2})(?:$|[\s_.-])", clean_line)
    if m:
        cand = m.group(1)
        if cand.upper().replace("-", "_") in EXCEL_LANGUAGE_CODES:
            language_code = cand
    if not language_code:
        m_short = re.search(r"(?:^|[\s_.-])([a-zA-Z]{2})(?:$|[\s_.-])", clean_line)
        if m_short:
            cand = m_short.group(1)
            if cand.upper() in EXCEL_LANGUAGE_CODES:
                language_code = cand

    # 5. Manual Type Code (e.g. IOM, DS, QSG, UI, etc.)
    all_codes = set(v.upper() for v in MANUAL_TYPE_CODE_MAP.values())
    tokens = re.split(r"[_\s\.-]+", clean_line)
    for tok in tokens:
        if tok.upper() in all_codes:
            manual_type_code = tok
            break

    # 6. Product Name
    rem = clean_line
    for item in [doc_number, version, date_code, language_code, manual_type_code]:
        if item:
            rem = re.sub(rf"(?:^|[\s_.-]){re.escape(item)}(?=$|[\s_.-])", " ", rem, count=1)
    product_name = re.sub(r"^[_\s\.-]+|[_\s\.-]+$", "", rem)
    product_name = re.sub(r"\s+", " ", product_name).strip() or None

    is_valid_footer = bool(doc_number or version or language_code or date_code or manual_type_code)

    return {
        "raw_line": clean_line,
        "doc_number": doc_number,
        "version": version,
        "language_code": language_code,
        "date_code": date_code,
        "manual_type_code": manual_type_code,
        "product_name": product_name,
        "is_valid_footer": is_valid_footer
    }


# =========================================================
# VERIFY LAST PAGE CONTENT
# =========================================================

def verify_last_page(pdf_path, lang_code="EN", ref_footer_model=None, ref_manual_type=None):
    """
    Verify Address, Disclaimer, Copyright, and Footer Line Metadata on the last page of a PDF.

    Parameters:
      pdf_path         : str  - Path to the PDF file
      lang_code        : str  - ISO Language Code (e.g. 'EN', 'DA', 'DE', 'SV')
      ref_footer_model : dict - Reference parsed footer dict from Master English PDF
      ref_manual_type  : str  - Master manual type full description (e.g. 'Installation, Operation, and Maintenance')

    Returns dict with detailed verification results.
    """
    doc = pymupdf.open(pdf_path)
    last_page_number = len(doc) - 1
    last_page = doc[last_page_number]
    last_page_text = last_page.get_text("text")
    page_normalized = normalize_text(last_page_text)

    # 1. Address Check (common for all languages)
    addr_normalized = normalize_text(address)
    has_address = addr_normalized in page_normalized

    # 2. Disclaimer Check (language mapped)
    target_disclaimer = get_disclaimer(lang_code)
    disc_normalized = normalize_text(target_disclaimer)
    has_disclaimer = disc_normalized in page_normalized
    if not has_disclaimer and isinstance(disclaimer_text, dict):
        for val in disclaimer_text.values():
            if normalize_text(val) in page_normalized:
                has_disclaimer = True
                break

    # 3. Copyright Check (language mapped)
    target_copyright = get_copyright(lang_code)
    copy_normalized = normalize_text(target_copyright)
    has_copyright = copy_normalized in page_normalized
    if not has_copyright and isinstance(copy_rights, dict):
        for val in copy_rights.values():
            if normalize_text(val) in page_normalized:
                has_copyright = True
                break

    # 4. Extract & Parse Last Line Footer Metadata
    lines = [line.strip() for line in last_page_text.splitlines() if line.strip()]
    last_line_text = lines[-1] if lines else ""
    parsed_footer = parse_footer_line(last_line_text)

    # Determine Expected Manual Type Code from reference manual type string
    expected_manual_type_code = None
    if ref_manual_type and ref_manual_type in MANUAL_TYPE_CODE_MAP:
        expected_manual_type_code = MANUAL_TYPE_CODE_MAP[ref_manual_type]
    elif ref_footer_model and ref_footer_model.get("manual_type_code"):
        expected_manual_type_code = ref_footer_model.get("manual_type_code")

    # Manual Type Code Check
    extracted_type_code = parsed_footer.get("manual_type_code")
    if expected_manual_type_code:
        manual_type_code_pass = bool(extracted_type_code and (extracted_type_code.upper() == expected_manual_type_code.upper()))
    else:
        manual_type_code_pass = bool(extracted_type_code)

    # Date Code Check (Compare with Master English PDF Date Code)
    expected_date_code = ref_footer_model.get("date_code") if ref_footer_model else None
    extracted_date_code = parsed_footer.get("date_code")
    if expected_date_code:
        date_code_pass = bool(extracted_date_code and (extracted_date_code == expected_date_code))
    else:
        date_code_pass = bool(extracted_date_code)

    # Language Code Check in Footer Line
    extracted_lang_code = parsed_footer.get("language_code")
    lang_code_pass = bool(extracted_lang_code)

    doc.close()

    # Overall verdict
    overall_pass = (
        has_address and
        has_disclaimer and
        has_copyright and
        parsed_footer["is_valid_footer"] and
        manual_type_code_pass and
        date_code_pass and
        lang_code_pass
    )

    return {
        "filename": os.path.basename(pdf_path),
        "last_page_number": last_page_number + 1,
        "language_code": lang_code,
        "has_address": has_address,
        "has_disclaimer": has_disclaimer,
        "has_copyright": has_copyright,
        "address_status": "Matched" if has_address else "Not Matched",
        "disclaimer_status": "Matched" if has_disclaimer else "Not Matched",
        "copyright_status": "Matched" if has_copyright else "Not Matched",
        "footer_raw_line": last_line_text,
        "footer_doc_number": parsed_footer["doc_number"],
        "footer_version": parsed_footer["version"],
        "footer_language_code": extracted_lang_code,
        "footer_date_code": extracted_date_code,
        "footer_manual_type_code": extracted_type_code,
        "footer_product_name": parsed_footer["product_name"],
        "expected_manual_type_code": expected_manual_type_code,
        "expected_date_code": expected_date_code,
        "manual_type_code_status": f"PASS ({extracted_type_code})" if manual_type_code_pass else f"FAIL ({extracted_type_code or 'Missing'})",
        "date_code_status": f"PASS ({extracted_date_code})" if date_code_pass else f"FAIL ({extracted_date_code or 'Missing'})",
        "lang_code_status": f"PASS ({extracted_lang_code})" if lang_code_pass else "FAIL (Missing)",
        "overall_verdict": "PASS" if overall_pass else "FAIL",
    }
