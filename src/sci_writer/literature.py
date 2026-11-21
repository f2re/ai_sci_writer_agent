"""A small real Crossref metadata client. Retrieval is not full-text verification."""
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


def _clean(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def normalize_work(work: dict, index: int) -> dict:
    """Keep incomplete records as candidates; never silently invent authors or access."""
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
        parts = work.get(key, {}).get("date-parts", [])
        if parts and parts[0] and isinstance(parts[0][0], int):
            year = parts[0][0]
            break
    types = {"journal-article": "article", "book": "book", "book-chapter": "chapter", "dissertation": "thesis", "posted-content": "preprint", "dataset": "dataset", "standard": "standard"}
    doi = work.get("DOI")
    return {
        "candidate_index": index, "title": _clean(title[0]) if isinstance(title, list) and title else "",
        "authors": authors, "year": year, "doi": doi,
        "url": "https://doi.org/" + doi if isinstance(doi, str) and doi else work.get("URL"),
        "type": types.get(work.get("type"), "other"), "access": "metadata_only",
        "metadata_verified": False, "status": "candidate", "tradition": "undetermined",
        "is_foundational": False, "retrieved_from": "crossref", "retrieved_at": now(),
        "verification_needed": "Проверить автора/название/издание по издателю; отдельно получить и прочитать текст.",
    }


def crossref_search(query: str, *, rows: int = 10, mailto: str | None = None, timeout: float = 20.0) -> dict:
    if not query.strip() or not 1 <= rows <= 100:
        raise ContractError("Crossref: непустой запрос и rows от 1 до 100 обязательны")
    params = {"query.bibliographic": query, "rows": rows}
    if mailto:
        params["mailto"] = mailto
    url = API + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "ai-sci-writer-agent/0.1 (https://github.com/f2re/ai_sci_writer_agent)" + ("; mailto:" + mailto if mailto else ""),
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(12_000_001)
        if len(raw) > 12_000_000:
            raise ContractError("Crossref: ответ превышает лимит")
        payload = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"Crossref недоступен; поиск не считается выполненным: {exc}") from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("status") != "ok" or not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise ContractError("Crossref: неожиданный формат ответа")
    if any(not isinstance(item, dict) for item in message["items"]):
        raise ContractError("Crossref: повреждённая запись")
    return {
        "database": "Crossref", "query": query, "searched_at": now(), "request_url": url,
        "total_results": message.get("total-results"), "returned": len(message["items"]),
        "candidates": [normalize_work(item, index) for index, item in enumerate(message["items"], 1)],
        "raw_response_sha256": hashlib.sha256(raw).hexdigest(), "raw_response": payload,
        "scope": "metadata_candidates_only; not source inclusion, full-text access, or claim verification",
    }


def search_into_run(run: Path, query: str, *, rows: int = 10, mailto: str | None = None) -> dict:
    if not (run / "brief.json").is_file():
        raise ContractError("Сначала инициализируйте запуск")
    result = crossref_search(query, rows=rows, mailto=mailto)
    suffix = uuid4().hex[:10]
    path = safe_path(run, f"search/crossref-{suffix}.json")
    write_json(path, result)
    log = read_jsonl(run / "search-log.jsonl")
    log.append({key: value for key, value in result.items() if key not in ("raw_response", "candidates")})
    log[-1]["candidate_file"] = str(path.relative_to(run))
    write_text(run / "search-log.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in log))
    return {"candidate_file": str(path), "returned": result["returned"], "total_results": result["total_results"], "status": "metadata_retrieved"}

