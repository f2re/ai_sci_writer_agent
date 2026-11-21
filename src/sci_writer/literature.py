"""Real metadata clients and local BibTeX/RIS import, never full-text verification."""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4

from .contracts import ContractError, now, read_jsonl, safe_path, write_json, write_text

API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"
MAX_RESPONSE_BYTES = 12_000_000
MAX_IMPORT_BYTES = 5_000_000


def _clean(value: str) -> str:
    return html.unescape(re.sub(r"</?[A-Za-z][^>]*>", "", value)).strip()


def _candidate_id(record: dict) -> str:
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(record.get("doi") or ""), flags=re.I).lower().strip()
    material = ("doi:" + doi) if doi else json.dumps({key: record.get(key) for key in ("title", "authors", "year", "type")}, sort_keys=True, ensure_ascii=False)
    return "cand_" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _candidate(record: dict, index: int, provider: str) -> dict:
    result = {**record, "candidate_index": index, "access": "metadata_only", "metadata_verified": False,
              "status": "candidate", "tradition": "undetermined", "is_foundational": False,
              "retrieved_from": provider, "retrieved_at": now(),
              "verification_needed": "Проверить автора/название/издание по издателю; отдельно получить и прочитать текст."}
    result["candidate_id"] = _candidate_id(result)
    return result


def normalize_work(work: dict, index: int) -> dict:
    """Keep incomplete records as candidates; never invent authors or content access."""
    title = work.get("title", [])
    authors = []
    for author in work.get("author", []):
        if not isinstance(author, dict):
            continue
        name = " ".join(str(author.get(k, "")).strip() for k in ("given", "family")).strip()
        if name or author.get("name"):
            authors.append(name or str(author["name"]))
    year = None
    for key in ("published", "published-print", "published-online", "issued"):
        value = work.get(key) or {}
        parts = value.get("date-parts", []) if isinstance(value, dict) else []
        if parts and isinstance(parts[0], list) and parts[0] and isinstance(parts[0][0], int):
            year = parts[0][0]; break
    types = {"journal-article": "article", "book": "book", "book-chapter": "chapter", "dissertation": "thesis", "posted-content": "preprint", "dataset": "dataset", "standard": "standard"}
    doi = work.get("DOI")
    record = {"title": _clean(title[0]) if isinstance(title, list) and title else "", "authors": authors, "year": year,
              "doi": doi, "url": "https://doi.org/" + doi if isinstance(doi, str) and doi else work.get("URL"),
              "type": types.get(work.get("type"), "other")}
    for key, dest in (("volume", "volume"), ("issue", "issue"), ("page", "pages"), ("publisher", "publisher"), ("article-number", "article_number"), ("language", "language")):
        if work.get(key):
            record[dest] = str(work[key])
    for key, dest in (("container-title", "booktitle" if record["type"] == "chapter" else "journal"), ("ISBN", "isbn"), ("ISSN", "issn")):
        values = work.get(key)
        if isinstance(values, list) and values:
            record[dest] = _clean(str(values[0]))
    if work.get("abstract"):
        record["abstract"] = _clean(str(work["abstract"]))
    return _candidate(record, index, "crossref")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_json(url: str, provider: str, *, headers: dict | None = None, timeout: float = 20.0) -> tuple[dict, bytes]:
    """Fixed HTTPS provider endpoints; no proxy environment or credential redirects."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "ai-sci-writer-agent/0.2 (https://github.com/f2re/ai_sci_writer_agent)", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ContractError(f"{provider}: ответ превышает лимит")
        payload = json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ContractError(f"{provider}: HTTP {exc.code}; поиск не считается выполненным (проверьте ключ/квоту/доступ)") from None
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError):
        # Do not echo request headers, query credentials, or arbitrary server error bodies.
        raise ContractError(f"{provider} недоступен или вернул некорректный JSON; поиск не считается выполненным") from None
    if not isinstance(payload, dict):
        raise ContractError(f"{provider}: ожидается JSON object")
    return payload, raw


def crossref_search(query: str, *, rows: int = 10, mailto: str | None = None, timeout: float = 20.0) -> dict:
    if not query.strip() or not 1 <= rows <= 100:
        raise ContractError("Crossref: непустой запрос и rows от 1 до 100 обязательны")
    params = {"query.bibliographic": query, "rows": rows}
    if mailto:
        params["mailto"] = mailto
    url = API + "?" + urllib.parse.urlencode(params)
    payload, raw = _fetch_json(url, "Crossref", timeout=timeout)
    message = payload.get("message")
    if payload.get("status") != "ok" or not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise ContractError("Crossref: неожиданный формат ответа")
    if any(not isinstance(item, dict) for item in message["items"]):
        raise ContractError("Crossref: повреждённая запись")
    return {"database": "Crossref", "query": query, "searched_at": now(), "request_url": url,
            "total_results": message.get("total-results"), "returned": len(message["items"]),
            "candidates": [normalize_work(item, index) for index, item in enumerate(message["items"], 1)],
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(), "raw_response": payload,
            "scope": "metadata_candidates_only; not source inclusion, full-text access, or claim verification"}


def _abstract(index: dict | None) -> str | None:
    if not index:
        return None
    positions = {}
    if not isinstance(index, dict):
        raise ContractError("OpenAlex: abstract_inverted_index должен быть объектом")
    for word, offsets in index.items():
        if not isinstance(word, str) or not isinstance(offsets, list):
            raise ContractError("OpenAlex: повреждённый индекс аннотации")
        for offset in offsets:
            if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset < 100_000 or offset in positions:
                raise ContractError("OpenAlex: некорректная позиция аннотации")
            positions[offset] = word
    return " ".join(positions[i] for i in sorted(positions)) or None


def normalize_openalex_work(work: dict, index: int) -> dict:
    authorships = work.get("authorships") or []
    authors = [str(item["author"]["display_name"]) for item in authorships if isinstance(item, dict) and isinstance(item.get("author"), dict) and item["author"].get("display_name")]
    location = work.get("primary_location") or {}
    journal = location.get("source") or {}
    biblio = work.get("biblio") or {}
    types = {"article": "article", "book": "book", "book-chapter": "chapter", "dissertation": "thesis", "preprint": "preprint", "dataset": "dataset", "standard": "standard", "software": "software"}
    record = {"title": _clean(str(work.get("display_name") or work.get("title") or "")), "authors": authors,
              "year": work.get("publication_year"), "doi": work.get("doi"), "openalex_id": work.get("id"),
              "url": work.get("doi") or location.get("landing_page_url") or work.get("id"), "type": types.get(work.get("type"), "other")}
    for value, field in ((journal.get("display_name"), "journal"), (biblio.get("volume"), "volume"), (biblio.get("issue"), "issue"), (work.get("language"), "language")):
        if value:
            record[field] = str(value)
    first, last = biblio.get("first_page"), biblio.get("last_page")
    if first:
        record["pages"] = str(first) + ("–" + str(last) if last and str(last) != str(first) else "")
    abstract = _abstract(work.get("abstract_inverted_index"))
    if abstract:
        record["abstract"] = abstract
    oa = work.get("best_oa_location") or {}
    if oa.get("pdf_url"):
        record["pdf_url"] = oa["pdf_url"]
    if work.get("is_retracted") or work.get("is_pulled"):
        record["import_warnings"] = ["Retracted/withdrawn/pulled record: check publisher status before any use."]
    return _candidate({key: value for key, value in record.items() if value is not None or key in ("doi", "year", "url")}, index, "openalex")


def openalex_search(query: str, *, rows: int = 10, api_key: str | None = None, language: str | None = None, timeout: float = 20.0) -> dict:
    if not query.strip() or not 1 <= rows <= 100:
        raise ContractError("OpenAlex: непустой запрос и rows от 1 до 100 обязательны")
    params = {"search": query, "per-page": rows}
    if language:
        if not re.fullmatch(r"[a-z]{2}", language):
            raise ContractError("OpenAlex language: двухбуквенный код, например ru")
        params["filter"] = "language:" + language
    if api_key and ("\n" in api_key or "\r" in api_key):
        raise ContractError("OpenAlex: некорректный ключ")
    url = OPENALEX_API + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": "Bearer " + api_key} if api_key else {}
    payload, raw = _fetch_json(url, "OpenAlex", headers=headers, timeout=timeout)
    rows_data, meta = payload.get("results"), payload.get("meta")
    if not isinstance(rows_data, list) or any(not isinstance(row, dict) for row in rows_data) or not isinstance(meta, dict):
        raise ContractError("OpenAlex: неожиданный формат ответа")
    return {"database": "OpenAlex", "query": query, "searched_at": now(), "request_url": url,
            "authentication": "api_key_header" if api_key else "anonymous_first_page", "total_results": meta.get("count"),
            "returned": len(rows_data), "candidates": [normalize_openalex_work(item, index) for index, item in enumerate(rows_data, 1)],
            "raw_response_sha256": hashlib.sha256(raw).hexdigest(), "raw_response": payload,
            "scope": "metadata_candidates_only; abstracts and PDF URLs are not proof of read full text"}


def _store_search(run: Path, result: dict, prefix: str) -> dict:
    if not safe_path(run, "brief.json").is_file():
        raise ContractError("Сначала инициализируйте запуск")
    path = safe_path(run, f"search/{prefix}-{uuid4().hex[:10]}.json")
    write_json(path, result)
    log_path = safe_path(run, "search-log.jsonl")
    log = read_jsonl(log_path)
    log.append({key: value for key, value in result.items() if key not in ("raw_response", "candidates", "raw_import")})
    log[-1]["candidate_file"] = str(path.relative_to(run.resolve()))
    write_text(log_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in log))
    return {"candidate_file": str(path), "returned": result["returned"], "total_results": result.get("total_results", result["returned"]), "status": "metadata_retrieved"}


def search_into_run(run: Path, query: str, *, rows: int = 10, mailto: str | None = None) -> dict:
    if not safe_path(run, "brief.json").is_file():
        raise ContractError("Сначала инициализируйте запуск")
    return _store_search(run, crossref_search(query, rows=rows, mailto=mailto), "crossref")


def search_openalex_into_run(run: Path, query: str, *, rows: int = 10, api_key: str | None = None, language: str | None = None) -> dict:
    if not safe_path(run, "brief.json").is_file():
        raise ContractError("Сначала инициализируйте запуск")
    return _store_search(run, openalex_search(query, rows=rows, api_key=api_key, language=language), "openalex")


class _BibParser:
    """Subset of BibTeX export syntax with nested braces, strings and concatenation.

    Unknown macros, duplicate fields, unmatched braces and crossref inheritance
    fail explicitly instead of silently changing bibliographic data. No TeX runs.
    """
    def __init__(self, text: str):
        self.text, self.pos = text, 0
        self.macros = {name: name.title() for name in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")}

    def skip(self):
        while self.pos < len(self.text):
            if self.text[self.pos].isspace():
                self.pos += 1
            elif self.text[self.pos] == "%":
                end = self.text.find("\n", self.pos)
                self.pos = len(self.text) if end == -1 else end + 1
            else:
                break

    def token(self, pattern=r"[A-Za-z0-9_:.+/-]+"):
        self.skip()
        match = re.match(pattern, self.text[self.pos:])
        if not match:
            raise ContractError(f"BibTeX: неожиданный токен в позиции {self.pos}")
        self.pos += len(match[0]); return match[0]

    def expect(self, value):
        self.skip()
        if self.pos >= len(self.text) or self.text[self.pos] != value:
            raise ContractError(f"BibTeX: ожидался {value!r} в позиции {self.pos}")
        self.pos += 1

    def atom(self):
        self.skip()
        if self.pos >= len(self.text):
            raise ContractError("BibTeX: незавершённое значение")
        first = self.text[self.pos]
        if first in ('{', '"'):
            self.pos += 1; depth = 1 if first == '{' else 0; out = []
            while self.pos < len(self.text):
                char = self.text[self.pos]; self.pos += 1
                if char == "\\" and self.pos < len(self.text):
                    out.extend((char, self.text[self.pos])); self.pos += 1; continue
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth < 0:
                        raise ContractError("BibTeX: несогласованные фигурные скобки")
                    if first == '{' and depth == 0:
                        return "".join(out)
                elif char == '"' and first == '"' and depth == 0:
                    return "".join(out)
                out.append(char)
            raise ContractError("BibTeX: незакрытое значение")
        value = self.token()
        if value.isdigit():
            return value
        if value.lower() in self.macros:
            return self.macros[value.lower()]
        raise ContractError(f"BibTeX: неопределённый string-макрос {value!r}")

    def value(self):
        result = self.atom(); self.skip()
        while self.pos < len(self.text) and self.text[self.pos] == "#":
            self.pos += 1; result += self.atom(); self.skip()
        return result

    def parse(self):
        rows = []; keys = set()
        while True:
            self.skip()
            if self.pos == len(self.text):
                return rows
            self.expect('@'); kind = self.token().lower(); self.skip()
            if self.pos >= len(self.text) or self.text[self.pos] not in '{(':
                raise ContractError("BibTeX: ожидалась скобка записи")
            closer = '}' if self.text[self.pos] == '{' else ')'; self.pos += 1
            if kind in ("comment", "preamble"):
                raise ContractError("BibTeX: удалите @comment/@preamble перед импортом; эти директивы не исполняются")
            if kind == "string":
                name = self.token().lower(); self.expect('='); self.macros[name] = self.value(); self.skip()
                if self.pos < len(self.text) and self.text[self.pos] == ',': self.pos += 1
                self.expect(closer); continue
            key = self.token(r"[^\s,{}()]+"); self.expect(','); fields = {}
            if key in keys:
                raise ContractError(f"BibTeX: повторный ключ {key}")
            keys.add(key)
            while True:
                self.skip()
                if self.pos < len(self.text) and self.text[self.pos] == closer:
                    self.pos += 1; break
                name = self.token().lower(); self.expect('=')
                if name in fields:
                    raise ContractError(f"BibTeX: повторное поле {name} в {key}")
                fields[name] = self.value(); self.skip()
                if self.pos < len(self.text) and self.text[self.pos] == ',':
                    self.pos += 1
                elif self.pos >= len(self.text) or self.text[self.pos] != closer:
                    raise ContractError(f"BibTeX: ожидалась запятая после {name}")
            if "crossref" in fields:
                raise ContractError("BibTeX: crossref inheritance не поддерживается; экспортируйте полные записи")
            rows.append({"entry_type": kind, "entry_key": key, "fields": fields})


def _bib_value(value: str) -> str:
    # Keep unknown TeX literal and declare it in import_warnings, never execute it.
    value = re.sub(r"\\([&%_#{}$])", r"\1", value)
    value = value.replace("{", "").replace("}", "")
    return _clean(value)


def _split_authors(value: str) -> list[str]:
    # Corporate names protected by braces may contain the word 'and'.
    parts, buffer, depth = [], [], 0
    i = 0
    while i < len(value):
        char = value[i]
        if char == '{': depth += 1
        elif char == '}': depth -= 1
        if depth == 0 and re.match(r"\s+and\s+", value[i:], re.I):
            match = re.match(r"\s+and\s+", value[i:], re.I)
            parts.append(_bib_value(''.join(buffer))); buffer = []; i += len(match[0]); continue
        buffer.append(char); i += 1
    if buffer: parts.append(_bib_value(''.join(buffer)))
    return [part for part in parts if part]


def parse_bibtex(text: str) -> list[dict]:
    output = []
    kinds = {"article": "article", "book": "book", "inbook": "chapter", "incollection": "chapter", "inproceedings": "chapter", "phdthesis": "thesis", "mastersthesis": "thesis"}
    for index, entry in enumerate(_BibParser(text).parse(), 1):
        fields = entry['fields']
        clean = {key: _bib_value(value) for key, value in fields.items()}
        year = clean.get('year', '')
        doi = clean.get('doi')
        record = {"title": clean.get('title', ''), "authors": _split_authors(fields.get('author', '')),
                  "year": int(year) if re.fullmatch(r"\d{4}", year) else None,
                  "url": clean.get('url') or ("https://doi.org/" + re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I) if doi else None),
                  "doi": doi, "type": kinds.get(entry['entry_type'], 'other'), "import_key": entry['entry_key'], "raw_import_fields": fields}
        mapping = {'journal':'journal','volume':'volume','number':'issue','pages':'pages','address':'city','publisher':'publisher','edition':'edition','edn':'edn','booktitle':'booktitle','isbn':'isbn','issn':'issn','urldate':'accessed_at','abstract':'abstract','language':'language','eid':'article_number'}
        record.update({dest: clean[key] for key, dest in mapping.items() if clean.get(key)})
        if fields.get('editor'): record['editors'] = _split_authors(fields['editor'])
        warnings = []
        if year and record['year'] is None: warnings.append('Non-year date preserved in raw_import_fields.year; verify manually.')
        if any(re.search(r"\\[A-Za-z]", value) for value in fields.values()): warnings.append('TeX macros retained literally; verify decoded spelling manually.')
        if warnings: record['import_warnings'] = warnings
        output.append(_candidate(record, index, 'bibtex_import'))
    return output


def parse_ris(text: str) -> list[dict]:
    entries, current, last = [], None, None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip(): continue
        match = re.match(r"^([A-Z0-9]{2})  - ?(.*)$", line)
        if not match:
            if current is not None and last and line[:1].isspace():
                current[last][-1] += ' ' + line.strip(); continue
            raise ContractError(f"RIS:{number}: ожидалась строка TAG  - value")
        tag, value = match.groups()
        if tag == 'TY':
            if current is not None: raise ContractError(f"RIS:{number}: запись без ER")
            current = {'TY':[value]}
        elif tag == 'ER':
            if current is None: raise ContractError(f"RIS:{number}: ER без TY")
            entries.append(current); current = None
        else:
            if current is None: raise ContractError(f"RIS:{number}: поле вне записи")
            current.setdefault(tag, []).append(value)
        last = tag
    if current is not None: raise ContractError('RIS: последняя запись без ER')
    output = []
    for index, fields in enumerate(entries, 1):
        def one(*tags):
            return next((fields[tag][0].strip() for tag in tags if fields.get(tag)), '')
        year_match = re.match(r"(\d{4})", one('PY','Y1'))
        doi = one('DO') or None
        kind = {'JOUR':'article','BOOK':'book','CHAP':'chapter','THES':'thesis','DATA':'dataset','STAND':'standard','COMP':'software'}.get(one('TY'),'other')
        record = {'title':one('TI','T1'), 'authors':fields.get('AU') or fields.get('A1') or [], 'year':int(year_match[1]) if year_match else None,
                  'type':kind,'doi':doi,'url':one('UR') or ('https://doi.org/'+re.sub(r'^https?://(?:dx\.)?doi\.org/','',doi,flags=re.I) if doi else None), 'raw_import_fields':fields}
        for tags,dest in ((('JO','JF','JA','T2'),'booktitle' if kind=='chapter' else 'journal'),(('VL',),'volume'),(('IS',),'issue'),(('PB',),'publisher'),(('CY',),'city'),(('ET',),'edition'),(('AB','N2'),'abstract'),(('LA',),'language')):
            if one(*tags): record[dest]=one(*tags)
        if one('SP'): record['pages']=one('SP')+('–'+one('EP') if one('EP') and one('EP')!=one('SP') else '')
        if one('SN'): record['isbn' if kind in ('book','chapter') else 'issn']=one('SN')
        if fields.get('A2'): record['editors']=fields['A2']
        output.append(_candidate(record,index,'ris_import'))
    return output


def import_bibliography(run: Path, path: Path, *, format: str | None = None, encoding: str = 'utf-8-sig') -> dict:
    if not safe_path(run, 'brief.json').is_file(): raise ContractError('Сначала инициализируйте запуск')
    try:
        with path.open('rb') as handle: raw=handle.read(MAX_IMPORT_BYTES+1)
    except OSError as exc:
        raise ContractError(f'Не удалось открыть библиографию: {exc}') from exc
    if len(raw)>MAX_IMPORT_BYTES: raise ContractError('Библиографический файл превышает 5 МБ')
    try: text=raw.decode(encoding)
    except (LookupError,UnicodeError) as exc: raise ContractError('Неизвестная кодировка/ошибка декодирования; укажите реальную кодировку экспорта') from exc
    kind=(format or path.suffix.lstrip('.')).lower()
    if kind in ('bib','bibtex'): candidates=parse_bibtex(text); kind='bibtex'
    elif kind=='ris': candidates=parse_ris(text)
    else: raise ContractError('Поддерживаются .bib/.bibtex и .ris; укажите format явно')
    if not candidates: raise ContractError('Библиографический экспорт не содержит записей')
    unique={}
    for candidate in candidates: unique.setdefault(candidate['candidate_id'],candidate)
    result={'database':kind+'_import','query':path.name,'searched_at':now(),'returned':len(unique),'total_results':len(candidates),
            'duplicate_candidates_removed':len(candidates)-len(unique),'candidates':list(unique.values()),'raw_import':text,
            'raw_import_sha256':hashlib.sha256(raw).hexdigest(),'source_encoding':encoding,
            'scope':'metadata_candidates_only; source selection and reading remain separate'}
    return _store_search(run,result,kind+'-import')
