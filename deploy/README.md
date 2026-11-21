# LanguageTool и автономный профиль этапа 5

Контейнер обрабатывает текст на вашем хосте. Он доступен только через loopback;
фоновый запуск не открывает публичный сервис. Требуется Docker Compose.

```bash
docker compose -f deploy/compose.quality.yml up -d --wait
curl --fail http://127.0.0.1:8010/v2/languages
```

После установки проекта выполните интегрированную команду из корня репозитория:

```bash
sci-writer quality runs/my-article --languagetool-url http://127.0.0.1:8010/v2/check
```

Она читает brief, словарь и профиль автора, запускает проверки и связывает
отчёт с текущим контекстом workflow. Для импорта уже выполненной проверки
Главреда добавьте `--glavred-report путь/к/отчёту.json`.

Для интеграции в другой Python-процесс доступен низкоуровневый API:

```python
from sci_writer.quality import run_quality

report = run_quality(
    "runs/my-article/manuscript.md",
    output_dir="runs/my-article/reports",
    languagetool_url="http://127.0.0.1:8010/v2/check",
)
print(report["required_complete"])
```

Прямой `run_quality` выполняет проверки, но не добавляет workflow
`context_sha256`; его результат самостоятельно не закрывает этап 05.
Для workflow используйте CLI выше или `sci_writer.cli.quality_run`.

Подставьте существующий путь к рукописи. Пример не создаёт научный текст.
В профиле `strict_local` отсутствующий URL оставляет проверку LanguageTool в
состоянии `not_run` и не закрывает этап. В облачном CI этот же контейнер можно запустить на runner;
облачный runner должен иметь разрешённый доступ к соответствующей рукописи.
Для URL собственного сервиса вне loopback нужен явный `allow_remote=True`.
Перенаправления HTTP не выполняются, переменные окружения proxy игнорируются.
Не используйте бесплатный публичный API LanguageTool для автоматизации.

## Среда без Docker или сервисного доступа

Docker — один способ запустить LanguageTool, а не требование к любому host-агенту.
Допустим уже запущенный собственный Java/HTTP-сервер. Передача на удалённый сервер
по-прежнему требует разрешения в brief и явного флага `--allow-remote`.

Если сервис недоступен как возможность среды, задайте в brief
`"execution_profile": "offline_sandbox"` и выполните обычный `sci-writer quality`.
Локальные правила русского текста, тезауруса и базовая проверка EN-аннотации
работают без сети. Отсутствие конфигурации внешних сервисов записывается как
`skipped_no_runtime`; исходная квитанция содержит причину и хеш рукописи.
`required_complete` остаётся `false`, а допустимость ограниченного выпуска
отражается в `draft_quality_complete` и финальном манифесте.

Самопроверка и автономная вычитка не заявляются независимой рецензией или полной
сервисной проверкой. Такой выпуск не получает `ready_for_submission=true`.
Реальная ошибка вызванного сервиса не маскируется пропуском: исправьте URL/доступ,
повторите проверку либо явно выберите автономный маршрут и сохраните ограничения.

## Версия и воспроизводимость

В конфигурации указан опубликованный `erikvl87/languagetool:6.8`. Тег проверен
21 ноября 2026 года по [Docker Hub](https://hub.docker.com/r/erikvl87/languagetool/tags).
Версия `6.8` также указана в
[Dockerfile издателя](https://github.com/Erikvl87/docker-languagetool/blob/master/Dockerfile).
Образ community, он перечислен в
[официальном README LanguageTool](https://github.com/languagetool-org/languagetool).

Тег версии может быть перепубликован. Чтобы закрепить digest именно полученного
образа, сначала выполните pull, затем запросите реальное значение:

```bash
docker pull erikvl87/languagetool:6.8
docker image inspect erikvl87/languagetool:6.8 --format '{{json .RepoDigests}}'
```

Скопируйте выданную полную ссылку `erikvl87/languagetool@sha256:...` в поле
`image`, если нужна фиксация на уровне байтов. Репозиторий не содержит
выдуманного digest. `quality.json` отдельно записывает версию, сообщённую
проверяющим сервером, и хеши сырых ответов.

## Проверка и остановка

```bash
docker compose -f deploy/compose.quality.yml logs --tail 30 languagetool
docker compose -f deploy/compose.quality.yml down
```

Проверки протокола в `tests/test_quality.py` используют тестовый HTTP server.
Они не подтверждают запуск настоящего LanguageTool. Выполненный запуск
подтверждается только реальными ответами сервера в `reports/raw/`.

Настоящий контейнер LanguageTool 6.8 успешно проверен в GitHub Actions
21 ноября 2026 года: две намеренные ошибки русского тестового текста найдены,
целостность исходных ответов подтверждена. [Результаты и наблюдавшийся digest](../docs/validation.md).

Источники: [документация своего HTTP server](https://dev.languagetool.org/http-server),
[ограничения public API](https://dev.languagetool.org/public-http-api.html),
[документация образа](https://github.com/Erikvl87/docker-languagetool).
