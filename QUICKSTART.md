# Quickstart — «Обучающий центр / анализ собеседований»

Короткая инструкция по запуску процесса получения транскриптов по ссылкам на
собеседования / onboarding-созвоны бухгалтеров и сохранения результата.

Полная документация — в `README.md`, раздел **14**.

## 1. Что делает процесс

```
Google Sheet «Обучающий центр ОВ» (вкладка «Бух»)
   → берём ссылку на звонок/транскрипт
   → получаем ПОЛНЫЙ транскрипт: Timeless API → Google Docs → ручной файл
   → сохраняем raw + cleaned транскрипт в Supabase (OB FAQ)
   → ставим статус обработки (new → transcript_ready → analysis_done | error)
   → (опц.) AI-анализ кандидата + Telegram-отчёт
```

Транскрипты сохраняются **полностью и корректно** (не summary), потому что на
этих звонках принимается решение о приёме/обучении.

## 2. Разовая настройка (env на Render)

Сервис `ob-meeting-daily-report` уже содержит `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY` и AI-ключ. Для чтения Google-таблицы/документов
добавьте **одно** из:

- **Google Sheets API (рекомендуется):**
  `INTERVIEW_SPREADSHEET_ID=1pHlfGTYHYy54GKEGyMg9DbJowr27EBL1wVZTQkVQJP0`
  `GOOGLE_SERVICE_ACCOUNT_JSON=<содержимое ключа сервис-аккаунта>`
  и расшарьте таблицу + транскрипт-доки на email сервис-аккаунта (Viewer).
- **Или** локальный экспорт: скачайте лист как `.xlsx` и запускайте с `--xlsx`.

> Транскрипт-доки, открытые «по ссылке», читаются и **без** сервис-аккаунта
> (публичный export). Это проверено на реальных доках Роберта и Давита.

## 3. Запуск

```bash
# Полный прогон из настроенной Google-таблицы (транскрипт + анализ + сохранение):
python scripts/sync_interviews.py

# Из локального .xlsx (вкладка по умолчанию «Бух»):
python scripts/sync_interviews.py --xlsx ./export.xlsx --tab Бух

# Только транскрипты, без AI-анализа:
python scripts/sync_interviews.py --no-analyze

# Заново обработать уже завершённые (новая версия анализа):
python scripts/sync_interviews.py --force

# Прогнать N строк для проверки:
python scripts/sync_interviews.py --limit 5
```

Скрипт печатает JSON-итог (`processed`, `analysis_done`, `errors`, `counts`,
`run_id`). Каждый шаг пишется в таблицу `intv_sync_logs`.

## 4. Автоматический запуск по расписанию (Render cron)

`render.yaml` определяет два cron-сервиса (время в UTC; Armenia = UTC+4):

| Сервис | Расписание | Что делает |
| ------ | ---------- | ---------- |
| `ob-interview-sync-full` | `0 6 1 * *` — **1-е число**, 10:00 Ереван (**~раз в 30 дней**) | Полный прогон: таблица → транскрипты → **AI-анализ** → сохранение |
| `ob-interview-sync-mini` | `0 6 16 * *` — **16-е число**, 10:00 Ереван (**~раз в 15 дней**) | Мини-обновление: обновить кандидатов + забрать **новые** транскрипты (`--no-analyze`, без расходов на AI) |

Вместе они дают обновление примерно **каждые 15 дней** и полный
проанализированный прогон **каждые 30 дней**. Прогон идемпотентен: дубликаты не
создаются, уже завершённые собеседования пропускаются.

**Подключение:** Render Dashboard → **New → Blueprint** → выбрать этот репозиторий
(ветка `main`) → Render создаст оба сервиса из `render.yaml`. Затем на каждом
сервисе задать секреты (`SUPABASE_*`, `GEMINI_API_KEY`/`ANTHROPIC_API_KEY`,
`INTERVIEW_SPREADSHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`) — либо привязать оба к
общей **Environment Group**, чтобы вводить один раз. Изменить периодичность можно
в поле `schedule` (cron-выражение).

## 5. Как проверить результат (Supabase OB FAQ → SQL editor)

```sql
-- статусы обработки
select status, count(*) from intv_interviews group by status;

-- сохранённые транскрипты
select c.full_name, i.interview_type, i.transcript_source, t.char_count
from intv_transcripts t
join intv_interviews i on i.id = t.interview_id
join intv_candidates c on c.id = i.candidate_id;

-- кандидаты по треку и решению
select track, decision_status, count(*) from intv_candidates group by 1,2 order by 1;
```
