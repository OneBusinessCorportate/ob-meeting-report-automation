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

## 4. Автоматический запуск по расписанию (БЕЗ нового Render-сервиса)

Всё работает в **уже существующем** cron-сервисе `ob-meeting-daily-report`
(ветка `main`). Создавать новый сервис/проект НЕ нужно.

Этот сервис запускается каждый будний день и выполняет `scripts/run_daily.py`,
который делает две вещи независимо:
1. утренний отчёт по встрече (как раньше);
2. проверку расписания собеседований — и запускает синхронизацию, **только
   если подошёл срок**:

| Тип | Периодичность | Что делает |
| --- | ------------- | ---------- |
| **FULL** | **~раз в 30 дней** | Таблица → транскрипты → **AI-анализ** → сохранение. Сбрасывает таймеры full и mini. |
| **MINI** | **~раз в 15 дней** | Обновить кандидатов + забрать **новые** транскрипты (без AI). |

Периодичность **по времени**, а не по календарным числам: срок считается от
последнего успешного запуска (метки в `intv_sync_logs`), поэтому даже если день
выпал на выходной, синхронизация выполнится в первый же будний прогон после
порога. Идемпотентно: дубликаты не создаются, готовые собеседования пропускаются.

**Что нужно сделать:** просто задать на этом сервисе секреты
`INTERVIEW_SPREADSHEET_ID` и `GOOGLE_SERVICE_ACCOUNT_JSON` (Supabase и AI-ключ там
уже есть) и сделать **Manual Deploy → Deploy latest commit**. Больше ничего.

Периодичность можно переопределить переменными `INTERVIEW_FULL_INTERVAL_DAYS`
(по умолчанию 30) и `INTERVIEW_MINI_INTERVAL_DAYS` (15).

Запуск вручную (по желанию):
```bash
python scripts/run_interview_schedule.py            # запустить, если подошёл срок
python scripts/run_interview_schedule.py --kind full   # принудительно полный
python scripts/run_interview_schedule.py --kind mini   # принудительно мини
```

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
