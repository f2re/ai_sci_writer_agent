"""Journal metadata and transparent publication exports; never a readiness decision.

The GOST-based formatter covers common article/book/chapter records. Journal rules
and exceptional bibliography types still require editorial verification.
"""
from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .contracts import ContractError, safe_path, schema_errors, write_text

CITATION_STYLES = ("gost-r-7.0.5-2008", "numeric")


def publication_errors(metadata: dict, required_fields: tuple[str, ...] | list[str] = ()) -> list[str]:
    errors = schema_errors(metadata, "publication", "publication")
    if errors:
        return errors
    ids = [entry["id"] for entry in metadata.get("affiliations", [])]
    if len(ids) != len(set(ids)):
        errors.append("publication: повторный id аффилиации")
    for author in metadata.get("authors", []):
        for identifier in author.get("affiliation_ids", []):
            if identifier not in ids:
                errors.append(f"publication: неизвестная аффилиация {identifier}")
    if not isinstance(required_fields, (tuple, list)) or any(not isinstance(field, str) or not field.strip() for field in required_fields):
        return errors + ["publication_required_fields: ожидается список непустых путей полей"]
    for field in required_fields:
        value = metadata
        for part in field.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value in (None, "", [], {}):
            errors.append(f"publication: журнал требует поле {field}")
    return errors


def _text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sentence(value: str) -> str:
    return value.rstrip(". ") + "." if value else ""


def _date(value: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value)
    return f"{match[3]}.{match[2]}.{match[1]}" if match else value


def format_reference(source: dict, style: str = "gost-r-7.0.5-2008") -> str:
    """Format provided metadata without inventing absent volume/pages/publisher."""
    if style not in CITATION_STYLES:
        raise ContractError(f"Неизвестный стиль библиографии: {style}")
    authors = ", ".join(_text(author) for author in source.get("authors", []))
    title = _text(source.get("title"))
    kind = source.get("type", "other")
    year = str(source["year"]) if source.get("year") is not None else "б. г."
    parts = []
    if authors:
        parts.append(_sentence(authors))
    if kind in ("article", "chapter"):
        container = _text(source.get("journal") if kind == "article" else source.get("booktitle"))
        parts.append(_sentence(title + (" // " + container if container else "")))
    else:
        parts.append(_sentence(title))
    if source.get("edition"):
        parts.append(_sentence(_text(source["edition"])))
    if kind in ("book", "chapter", "thesis", "standard"):
        place, publisher = _text(source.get("city")), _text(source.get("publisher"))
        imprint = place + ": " + publisher if place and publisher else place or publisher
        parts.append(_sentence((imprint + ", " if imprint else "") + year))
    else:
        parts.append(_sentence(year))
    if source.get("volume"):
        parts.append(_sentence("Т. " + _text(source["volume"])))
    if source.get("issue"):
        parts.append(_sentence("№ " + _text(source["issue"])))
    if source.get("pages"):
        parts.append(_sentence("С. " + _text(source["pages"]).replace("--", "–")))
    if source.get("page_count"):
        parts.append(_sentence(str(source["page_count"]) + " с"))
    if source.get("article_number"):
        parts.append(_sentence("Ст. " + _text(source["article_number"])))
    if source.get("isbn"):
        parts.append(_sentence("ISBN " + _text(source["isbn"])))
    if source.get("doi"):
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", _text(source["doi"]), flags=re.I)
        parts.append(_sentence("DOI: " + doi))
    if source.get("edn"):
        parts.append(_sentence("EDN: " + _text(source["edn"])))
    url = _text(source.get("url"))
    # A DOI already identifies the DOI URL; preserve distinct publisher/catalog URLs.
    if url and not (source.get("doi") and re.match(r"https?://(?:dx\.)?doi\.org/", url, re.I)):
        access = f" (дата обращения: {_date(source['accessed_at'])})" if source.get("accessed_at") else ""
        parts.append("URL: " + url + access)
    if authors and len(parts) >= 2:
        parts = [parts[0] + " " + parts[1]] + parts[2:]
    if style == "numeric":
        return " ".join(part for part in parts if part)
    return " — ".join(part for part in parts if part)


def bibliography_bibtex(sources: list[dict]) -> str:
    def escape(value) -> str:
        return str(value).translate(str.maketrans({"\\": "\\textbackslash{}", "{": "\\{", "}": "\\}", "&": "\\&", "%": "\\%", "_": "\\_", "#": "\\#", "$": "\\$"}))

    def people(names):
        return " and ".join("{" + escape(name) + "}" if " and " in name.lower() else escape(name) for name in names)

    entries = []
    types = {"book": "book", "article": "article", "chapter": "incollection"}
    for source in sources:
        kind = types.get(source.get("type"), "misc")
        fields = {"title": source.get("title"), "author": people(source.get("authors", [])),
                  "year": source.get("year"), "doi": source.get("doi"), "url": source.get("url")}
        mapping = {"journal": "journal", "volume": "volume", "issue": "number", "pages": "pages",
                   "city": "address", "publisher": "publisher", "edition": "edition", "booktitle": "booktitle",
                   "isbn": "isbn", "issn": "issn", "edn": "edn", "accessed_at": "urldate", "language": "language"}
        fields.update({target: source[key] for key, target in mapping.items() if source.get(key)})
        if source.get("editors"):
            fields["editor"] = people(source["editors"])
        if source.get("article_number"):
            fields["eid"] = source["article_number"]
        identifier = source.get("id", "")
        if not re.fullmatch(r"S[0-9]{3,}", identifier):
            raise ContractError("BibTeX требует проверенный source id S001")
        entries.append("@" + kind + "{" + identifier + ",\n" + ",\n".join(
            "  " + key + " = {" + (value if key in ("author", "editor") else escape(value)) + "}" for key, value in fields.items() if value not in (None, "")) + "\n}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def publication_header(metadata: dict) -> str:
    """Render provided bilingual front matter; absent identifiers remain absent."""
    errors = publication_errors(metadata)
    if errors:
        raise ContractError("\n".join(errors))
    lines = []
    for key, label in (("udc", "УДК"), ("grnti", "ГРНТИ"), ("doi", "DOI"), ("edn", "EDN")):
        if metadata.get(key):
            lines.append(f"{label}: {_text(metadata[key])}")
    for lang in ("ru", "en"):
        if metadata.get("title", {}).get(lang):
            lines.append("# " + _text(metadata["title"][lang]))
        authors = []
        for author in metadata.get("authors", []):
            name = author.get("name_" + lang)
            if not name:
                continue
            aff = ", ".join(author.get("affiliation_ids", []))
            authors.append(_text(name) + (" (" + aff + ")" if aff else ""))
        if authors:
            lines.append("; ".join(authors))
        for affiliation in metadata.get("affiliations", []):
            name = affiliation.get("name_" + lang)
            if name:
                parts = [_text(name), _text(affiliation.get("city_" + lang)), _text(affiliation.get("country_" + lang))]
                lines.append("[" + affiliation["id"] + "] " + ", ".join(part for part in parts if part))
        abstract = metadata.get("abstract", {}).get(lang)
        if abstract:
            lines.append(("**Аннотация.** " if lang == "ru" else "**Abstract.** ") + abstract.strip())
        keywords = metadata.get("keywords", {}).get(lang, [])
        if keywords:
            lines.append(("**Ключевые слова:** " if lang == "ru" else "**Keywords:** ") + "; ".join(keywords))
    for lang in ("ru", "en"):
        details = []
        for author in metadata.get("authors", []):
            if not author.get("name_" + lang):
                continue
            bits = [author["name_" + lang], author.get("degree_" + lang), author.get("academic_title_" + lang)]
            bits += [label + ": " + author[key] for key, label in (("orcid", "ORCID"), ("spin", "SPIN"), ("email", "Email")) if author.get(key)]
            if author.get("contribution_" + lang):
                bits.append(("Вклад: " if lang == "ru" else "Contribution: ") + author["contribution_" + lang])
            if len([bit for bit in bits if bit]) > 1:
                details.append("; ".join(_text(bit) for bit in bits if bit))
        if details:
            lines.append("## " + ("Сведения об авторах" if lang == "ru" else "Author information"))
            lines.extend(details)
    for key, labels in (("funding", ("Финансирование", "Funding")), ("author_contributions", ("Вклад авторов", "Author contributions")),
                        ("conflicts", ("Конфликт интересов", "Conflicts of interest")), ("acknowledgements", ("Благодарности", "Acknowledgements"))):
        for lang, label in zip(("ru", "en"), labels):
            if metadata.get(key, {}).get(lang):
                lines.append("**" + label + ".** " + metadata[key][lang].strip())
    return "\n\n".join(lines) + ("\n\n" if lines else "")


def _inline_plain(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"[Иллюстрация: \1]", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    return text


def _blocks(markdown: str):
    """Conservative Markdown blocks: no HTML evaluation or external resource reads."""
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.lstrip().startswith("```"):
            index += 1
            block = []
            while index < len(lines) and not lines[index].lstrip().startswith("```"):
                block.append(lines[index]); index += 1
            yield "code", "\n".join(block)
            index += 1
            continue
        match = re.match(r"^(#{1,6})\s+(.+)", line)
        if match:
            yield "heading", (len(match[1]), match[2]); index += 1
            continue
        if index + 1 < len(lines) and "|" in line and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1]):
            def cells(row):
                return [cell.strip() for cell in row.strip().strip("|").split("|")]
            rows = [cells(line)]; index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = cells(lines[index])
                if len(row) != len(rows[0]):
                    raise ContractError("Таблица Markdown: число ячеек не совпадает с заголовком")
                rows.append(row); index += 1
            yield "table", rows
            continue
        match = re.match(r"^\s*(?:[-*+]\s+|(\d+)\.\s+)(.+)", line)
        if match:
            yield "number" if match[1] else "bullet", (match[1] + ". " if match[1] else "") + match[2]; index += 1
            continue
        paragraph = [line]
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(r"^(?:#{1,6}\s|```|[-*+]\s|\d+\.\s)", lines[index]):
            # Keep consecutive table start separate even without a blank line.
            if index + 1 < len(lines) and "|" in lines[index] and "---" in lines[index + 1]:
                break
            paragraph.append(lines[index]); index += 1
        yield "paragraph", " ".join(paragraph)


def _docx(markdown: str, path: Path, title: str | None) -> list[str]:
    try:
        from docx import Document
        from docx.shared import Cm, Pt
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
    except ImportError as exc:
        raise ContractError("DOCX: установите pip install -e '.[publishing]' (python-docx)") from exc
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2)
    section.left_margin = section.right_margin = Cm(2.5)
    for name in ("Normal", "Body Text", "List Bullet", "List Number"):
        style = document.styles[name]
        style.font.name, style.font.size = "Times New Roman", Pt(12)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.line_spacing = 1.15
    for name in ("Title", "Heading 1", "Heading 2", "Heading 3"):
        document.styles[name].font.name = "Times New Roman"
        document.styles[name].font.color.rgb = __import__("docx.shared", fromlist=["RGBColor"]).RGBColor(0, 0, 0)
    document.core_properties.title = title or ""
    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    warnings = []

    math_cache = {}

    def math_object(token):
        import copy
        import zipfile
        from lxml import etree
        if token in math_cache:
            return copy.deepcopy(math_cache[token]) if math_cache[token] is not None else None
        binary = shutil.which("pandoc")
        if not binary:
            warnings.append("docx_math_source_preserved: установите Pandoc для OMML или конвертируйте формулу вручную")
            math_cache[token] = None
            return None
        try:
            with tempfile.TemporaryDirectory(prefix="sci-math-", dir=path.parent) as folder:
                target = str(Path(folder) / "equation.docx")
                parsed = subprocess.run([binary, "--sandbox", "--from=markdown+tex_math_dollars+tex_math_single_backslash-raw_tex-raw_html", "--to=json"], input=token, text=True, capture_output=True, timeout=15, check=False)
                if parsed.returncode != 0:
                    raise ContractError("не удалось разобрать формулу")
                ast = json.loads(parsed.stdout)
                # Only one Math node, built into a fresh AST, can reach the writer.
                block = ast["blocks"][0]
                math = block["c"][0]
                if block["t"] not in ("Para", "Plain") or len(block["c"]) != 1 or math["t"] != "Math":
                    raise ContractError("неподдержанная математическая запись")
                ast = {"pandoc-api-version": ast["pandoc-api-version"], "meta": {}, "blocks": [{"t": "Para", "c": [math]}]}
                conversion = subprocess.run([binary, "--from=json", "--to=docx", "--output=" + target], input=json.dumps(ast, ensure_ascii=False), text=True, capture_output=True, timeout=20, check=False)
                if conversion.returncode or conversion.stderr.strip():
                    raise ContractError("формула не преобразована в Word OMML без предупреждений")
                with zipfile.ZipFile(target) as archive:
                    xml = etree.fromstring(archive.read("word/document.xml"))
                nodes = xml.findall(".//{http://schemas.openxmlformats.org/officeDocument/2006/math}oMath")
                if len(nodes) != 1:
                    raise ContractError("не найден однозначный объект OMML")
                math_cache[token] = nodes[0]
                return copy.deepcopy(nodes[0])
        except (OSError, subprocess.TimeoutExpired, ContractError, ValueError, KeyError, IndexError):
            warnings.append("docx_math_source_preserved: формула сохранена буквально; требуется проверка Word equations")
            math_cache[token] = None
            return None

    def prose(paragraph, value):
        for chunk in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", _inline_plain(value)):
            run = paragraph.add_run(chunk[2:-2] if chunk.startswith("**") and chunk.endswith("**") else chunk[1:-1] if chunk.startswith("`") and chunk.endswith("`") else chunk)
            if chunk.startswith("**"):
                run.bold = True
            elif chunk.startswith("`"):
                run.font.name = "Courier New"

    def inline(paragraph, value):
        offset = 0
        for match in _MATH.finditer(value):
            prose(paragraph, value[offset:match.start()])
            equation = math_object(match[0])
            if equation is not None:
                paragraph._p.append(equation)
            else:
                prose(paragraph, match[0])
            offset = match.end()
        prose(paragraph, value[offset:])

    for kind, value in _blocks(markdown):
        if kind == "table":
            table = document.add_table(rows=1, cols=len(value[0]))
            table.style = "Table Grid"
            table.autofit = False
            row_prop = table.rows[0]._tr.get_or_add_trPr()
            repeat = OxmlElement("w:tblHeader"); row_prop.append(repeat)
            for i, cell in enumerate(value[0]):
                inline(table.rows[0].cells[i].paragraphs[0], cell)
                for run in table.rows[0].cells[i].paragraphs[0].runs:
                    run.bold = True
            for row in value[1:]:
                cells = table.add_row().cells
                for i, cell in enumerate(row):
                    inline(cells[i].paragraphs[0], cell)
            for row in table.rows:
                for cell in row.cells:
                    cell.width = Cm(16 / len(table.columns))
            continue
        if kind == "heading":
            level, value = value
            paragraph = document.add_heading(level=min(level, 3))
            inline(paragraph, value)
        else:
            paragraph = document.add_paragraph(style={"bullet": "List Bullet"}.get(kind))
            if kind == "code":
                paragraph.add_run(value)
            else:
                inline(paragraph, value)
            if kind == "code":
                for run in paragraph.runs:
                    run.font.name, run.font.size = "Courier New", Pt(10)
    if re.search(r"!\[[^\]]*\]\(", markdown):
        warnings.append("external_images_not_loaded: экспорт не загружает изображения; проверьте локальную вёрстку рисунков")
    descriptor, temporary = tempfile.mkstemp(suffix=".docx", dir=path.parent)
    os.close(descriptor)
    try:
        document.save(temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return list(dict.fromkeys(warnings))


def _latex_escape(text: str) -> str:
    return text.translate(str.maketrans({"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}))


_SAFE_MATH_COMMANDS = set("alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega frac dfrac tfrac sqrt sum prod int iint iiint oint lim log ln exp sin cos tan cot arcsin arccos arctan sinh cosh tanh min max inf sup det gcd Pr left right le leq ge geq neq ne approx sim simeq equiv propto in notin subset subseteq supset supseteq cup cap emptyset forall exists neg land lor cdot times div pm mp partial nabla infty to rightarrow leftarrow leftrightarrow mapsto overline underline bar hat tilde vec dot ddot text mathrm mathbf mathit mathcal mathbb operatorname substack binom underbrace overbrace quad qquad displaystyle textstyle limits nolimits hspace vphantom phantom Big big Bigg bigg".split())
_MATH = re.compile(r"(?<!\\)(\$\$[\s\S]+?\$\$|\$[^$\n]+\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])")


def _latex_inline(value: str, warnings: list[str]) -> str:
    parts = []
    offset = 0
    for match in _MATH.finditer(value):
        parts.append(_latex_escape(_inline_plain(value[offset:match.start()]).replace("**", "")))
        token = match[0]
        width = 2 if token.startswith(("$$", "\\(", "\\[")) else 1
        body = token[width:-width]
        commands = re.findall(r"\\([A-Za-z]+)", body)
        depth = 0
        balanced = True
        for char in re.sub(r"\\[{}]", "", body):
            depth += 1 if char == "{" else -1 if char == "}" else 0
            if depth < 0: balanced = False
        safe = balanced and depth == 0 and all(command in _SAFE_MATH_COMMANDS for command in commands)
        # TeX expands ^^ character codes before control-sequence tokenization.
        # Reject that spelling before the command allowlist can be bypassed.
        safe = safe and "^^" not in body and not re.search(r"(?<!\\)[%#]", body)
        if safe:
            display = token.startswith(("$$", "\\["))
            parts.append((r"\[" if display else r"\(") + body + (r"\]" if display else r"\)"))
        else:
            parts.append(_latex_escape(token))
            warnings.append("tex_math_source_escaped: неподдержанная формула сохранена буквально; нужна ручная проверка")
        offset = match.end()
    parts.append(_latex_escape(_inline_plain(value[offset:]).replace("**", "")))
    return "".join(parts)


def _latex(markdown: str) -> tuple[str, list[str]]:
    """Escape author TeX; only a fixed mathematical command subset stays active."""
    lines = [r"\documentclass[12pt,a4paper]{article}", r"\usepackage{fontspec}", r"\setmainfont{DejaVu Serif}",
             r"\usepackage[russian,english]{babel}", r"\usepackage[margin=25mm]{geometry}", r"\usepackage{longtable}",
             r"\usepackage{array}", r"\usepackage{amsmath,amssymb}", r"\begin{document}"]
    warnings = []
    for kind, value in _blocks(markdown):
        if kind == "heading":
            level, value = value
            command = "section" if level <= 2 else "subsection" if level == 3 else "subsubsection"
            lines.append("\\" + command + "*{" + _latex_inline(value, warnings) + "}")
        elif kind == "table":
            width = 0.92 / len(value[0])
            lines.append(r"\begin{longtable}{" + "|" + "|".join(f"p{{{width:.3f}\\textwidth}}" for _ in value[0]) + "|}")
            lines.append(r"\hline")
            for row in value:
                lines.append(" & ".join(_latex_inline(cell, warnings) for cell in row) + r" \\ \hline")
            lines.append(r"\end{longtable}")
        elif kind == "bullet":
            lines.extend([r"\begin{itemize}", r"\item " + _latex_inline(value, warnings), r"\end{itemize}"])
        elif kind == "number":
            lines.append(_latex_inline(value, warnings) + "\n")
        elif kind == "code":
            lines.append(r"\begin{quote}\ttfamily " + r"\par ".join(_latex_escape(line) for line in value.splitlines()) + r"\end{quote}")
        else:
            lines.append(_latex_inline(value, warnings) + "\n")
    lines.append(r"\end{document}")
    if re.search(r"!\[[^\]]*\]\(", markdown):
        warnings.append("external_images_not_loaded: исходные рисунки не загружались")
    return "\n\n".join(lines) + "\n", warnings


def export_formats(markdown: str, output_dir: Path, formats: tuple[str, ...] | list[str] = ("docx", "tex"), *, title: str | None = None) -> dict:
    """Write optional formats; warnings must travel with the release manifest.

    This renderer never performs HTTP requests, loads images, compiles TeX, or
    treats raw Markdown/LaTeX as executable instructions. Optional Pandoc uses
    a sandboxed reader and a sanitized JSON AST for Word OMML. No scientific status
    is changed by successful conversion.
    """
    requested = list(dict.fromkeys(formats))
    if any(fmt not in ("docx", "tex", "latex") for fmt in requested):
        raise ContractError("Форматы публикации: docx, tex (или latex)")
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {"files": [], "warnings": [], "renderer": "sci_writer.publishing/0.2", "layout_verified": False}
    for fmt in requested:
        fmt = "tex" if fmt == "latex" else fmt
        path = safe_path(output_dir, "article." + fmt)
        if fmt == "docx":
            warnings = _docx(markdown, path, title)
        else:
            rendered, warnings = _latex(markdown)
            write_text(path, rendered)
        result["files"].append(path.name)
        result["warnings"].extend(warnings)
    return result
