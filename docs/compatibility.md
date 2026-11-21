# Совместимость с агентами и ограниченными средами

Рабочая политика находится в [AGENTS.md](../AGENTS.md), маршрутизация — в
[config/workflow.json](../config/workflow.json). Платформенные файлы содержат только указатели.
Наличие указателя не означает, что конкретная версия IDE уже прошла интеграционный тест.

## Входы разных платформ

| Среда | Файл входа | Как используются модули |
|---|---|---|
| Host, поддерживающий AGENTS.md | `AGENTS.md` | Координатор и файлы текущего этапа из `next` |
| Codex с локальным обнаружением навыков | `.agents/skills/sci-writer/SKILL.md` | Обнаруживаемый вход; рабочие навыки загружаются только по текущему этапу |
| Claude Code | `CLAUDE.md` с `@AGENTS.md` | Импорт общих правил, затем выборочное чтение навыков |
| Gemini CLI | `GEMINI.md` с `@./AGENTS.md` | Импорт общих правил, затем выборочное чтение навыков |
| GitHub Copilot | `.github/copilot-instructions.md` | Указатель на корневые правила и конфигурацию |
| Cursor | `.cursor/rules/sci-writer.mdc` | alwaysApply-указатель; архив исключён через `.cursorignore` |
| MCP-клиент без shell | `sci_writer.mcp_server` в доступной серверной среде | Те же контракты через транспорт MCP |
| Веб-чат без CLI/MCP | Явно прочитанные инструкции и материалы | Можно готовить материалы; детерминированные gates нельзя объявить выполненными |

Формат `skills/<name>/SKILL.md` соответствует [Agent Skills specification](https://agentskills.io/specification):
имя совпадает с каталогом, frontmatter содержит name/description, подробности открываются по запросу.
Это навыки **в составе репозитория**: они используют его схемы, CLI и артефакты. Копирование одного
SKILL.md без репозитория не устанавливает движок. Автообнаружение каталога `skills/` зависит от клиента;
при отсутствии такой поддержки host читает путь, возвращённый `next`.

Для Codex добавлен [обнаруживаемый вход sci-writer](../.agents/skills/sci-writer/SKILL.md).
Официальная документация описывает поиск `.agents/skills` от текущего каталога до корня репозитория
и начальную загрузку только имени/описания навыка. Вход направляет к общим правилам, координатору
и текущему этапу; **восемь рабочих модулей остаются в `skills/`**, новый вход не добавляет девятую роль.
Можно явно вызвать `$sci-writer` в клиенте, поддерживающем эту форму. Это repository skill;
он не устанавливает персональный навык ChatGPT и не создаёт запущенный MCP-сервер.

Проверены форматы файлов и ссылки. Сеансы всех перечисленных IDE в этой работе не запускались.
Правило на поддержку новой платформы: тонкий указатель, без копии всей научной политики.

## Профили исполнения

| Параметр | Полный режим | Ограниченный режим |
|---|---|---|
| `execution_profile` | `strict_local` по умолчанию | `offline_sandbox` |
| Этап 5 | Реальный локальный линтер и обязательный языковой сервис | Реальный локальный линтер; внешние пропуски честно `skipped_no_runtime` |
| `review_mode` | `independent_sessions` | `single_session_self_audit` |
| Рецензии | Отдельные domain/methods/chief сеансы | Те же профили как явно обозначенная самопроверка |
| Итог ограничений | Полная готовность только при выполнении всех gates | `reviewed_manuscript`, обе readiness-переменные false |

Параметры независимы: offline может иметь независимых рецензентов, а strict — только самопроверку.
Для offline ограничение `offline_quality`; для self-audit — `self_audit`; при сочетании — оба.
`independent_peer_review` отражает фактический режим рецензирования, а не число имён в отчётах.
В любом ограниченном режиме `scientific_ready=false` и `ready_for_submission=false`.
Открытые научные blocker/major и подмена отчётов блокируют принятие в любом профиле.

```bash
sci-writer init runs/sandbox-article --topic "Тема" --article-type review \
  --execution-profile offline_sandbox --review-mode single_session_self_audit
sci-writer next runs/sandbox-article
sci-writer quality runs/sandbox-article
sci-writer readiness runs/sandbox-article
```

Docker нужен только для выбранного контейнерного развёртывания LanguageTool. В strict допустим уже
работающий разрешённый сервер или реальный отчёт Главреда с настройкой required_tools.
Переключение профиля меняет бриф и контекст; прежние отчёты нужно повторно согласовать, а не переподписать.

## MCP и серверная среда

```bash
python -m pip install -e '.[mcp]'
python -m sci_writer.mcp_server --workspace /absolute/path/to/runs
```

MCP-сервер запускается на машине, где есть Python, репозиторий и доступ к рабочим файлам.
Он даёт транспорт к тем же функциям, а не обходит отсутствие исполнителя. Подключить этот процесс
через stdio-конфигурацию своего клиента; конкретное место настройки зависит от клиента.
Пути ограничены указанным workspace; передача текста сервису всё ещё требует согласованной политики.
Предоставлены 13 инструментов; MCP `status` содержит также пределы готовности из CLI `readiness`.
Подробный набор операций и пример конфигурации — в [MCP-интерфейсе](mcp.md).

## Гигиена контекста

Сначала загрузить глобальные правила, затем один текущий модуль. На этапе 6 выдать domain/methods
раздельным исполнителям; на этапе 7 загрузить chief. Не добавлять все восемь навыков в системный prompt.
`research-archive/` хранит исторические обзоры, а не текущие инструкции. `.cursorignore` исключает его
из обычного доступа Cursor, но терминал/MCP всё ещё могут прочитать его для явного аудита.

Для Claude реализован поддерживаемый эквивалент исключения архива —
[`.claude/settings.json`](../.claude/settings.json):

```json
{
  "permissions": {
    "deny": ["Read(/research-archive/**)"]
  }
}
```

В [официальных правилах доступа Claude Code](https://code.claude.com/docs/en/permissions#read-and-edit)
прямо указано, что `.claudeignore` не действует; вместо него используется `Read` deny rule.
Путь с `/` привязан к рабочему каталогу проекта в этой конфигурации: запускать Claude из корня checkout.
Правило адресовано только историческому архиву и не закрывает `docs/`, рабочие `runs/` или навыки.
`CLAUDE.md` сохраняет нативный импорт `@AGENTS.md` и требует выборочного чтения текущего этапа.

Запрет применяется к встроенным файловым инструментам; Claude также старается учитывать его
при Grep/Glob и IDE-контексте. Это **не универсальная граница ОС**: документация отдельно ограничивает
охват косвенного чтения из shell-процессов. Такие обходы не используются. Для явно запрошенного
исторического аудита владелец проекта должен изменить конкретное правило, а не обходить его инструментами.
Не добавляются фиктивный `.claudeignore`, запрет всего `docs/` или запрет рабочих материалов.

Это рабочая конфигурация документированного механизма; реальный UI-сеанс Claude для проверки
применения настройки в этой работе не запускался. Пакетные тесты проверяют точный узкий путь,
валидность JSON, нативный импорт правил и отсутствие загрузки архива в заданиях движка.

Официальные источники, сверенные 21.11.2026:

- [Claude Code memory/imports](https://code.claude.com/docs/en/memory).
- [Claude Code settings](https://code.claude.com/docs/en/settings) и [Read deny rules](https://code.claude.com/docs/en/permissions#read-and-edit).
- [OpenAI: обнаружение локальных Codex skills](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).
- [Gemini CLI GEMINI.md/imports](https://geminicli.com/docs/cli/gemini-md/).
- [Cursor rules](https://cursor.com/docs/rules) и [ignore file](https://cursor.com/docs/reference/ignore-file).
- [GitHub Copilot repository instructions](https://docs.github.com/en/copilot/how-tos/configure-custom-instructions/add-repository-instructions).
