# Diagnostics LangGraph — workflow

## Features
- **Iterative loop** — the LLM decides what to investigate next (max 4 iterations)
- **Persistent memory** — alert history saved to JSON, pattern detection across restarts

```mermaid
flowchart TD
    START([🚨 Alert fired]) --> load_history

    load_history["load_history\nчитает JSON с историей алертов"]
    collect_context["collect_context\nCPU / RAM / Disk / Load"]
    decide{"decide\n🤖 что собрать дальше?"}
    gather_processes["gather_processes\nтоп процессов по CPU"]
    gather_disk["gather_disk_detail\nper-disk stats + iotop"]
    gather_journal["gather_journal\nпоследние ошибки journal"]
    gather_docker["gather_docker\nстатус контейнеров"]
    detect_pattern["detect_pattern\n🔍 паттерн в истории?"]
    analyze["analyze\n🤖 root-cause анализ"]
    format_report["format_report\nсобирает сообщение"]
    save_history["save_history\nсохраняет в JSON"]
    END([📨 Telegram])

    load_history --> collect_context
    collect_context --> decide

    decide -->|gather_processes| gather_processes
    decide -->|gather_disk_detail| gather_disk
    decide -->|gather_journal| gather_journal
    decide -->|gather_docker| gather_docker
    decide -->|done| detect_pattern

    gather_processes -->|loop back| decide
    gather_disk -->|loop back| decide
    gather_journal -->|loop back| decide
    gather_docker -->|loop back| decide

    detect_pattern --> analyze
    analyze --> format_report
    format_report --> save_history
    save_history --> END

    style START fill:#e74c3c,color:#fff
    style END fill:#2ecc71,color:#fff
    style decide fill:#f39c12,color:#fff
    style analyze fill:#3498db,color:#fff
    style detect_pattern fill:#9b59b6,color:#fff
    style save_history fill:#1abc9c,color:#fff
```

## Как работает цикл

```
decide: "нужны процессы"
  → gather_processes → [добавил в state.gathered]
decide: "нужен journal"
  → gather_journal → [добавил в state.gathered]
decide: "достаточно"
  → detect_pattern → analyze → ...
```

Максимум 4 итерации (настраивается через `DIAGNOSTICS_MAX_ITER`).

## Как работает память

После каждого алерта `save_history` дописывает запись в
`/var/lib/system-monitor/alert_history.json`.

При следующем алерте `load_history` загружает историю, и `detect_pattern`
видит сколько раз этот тип алерта уже срабатывал за последние 24 часа.

Пример вывода когда паттерн обнаружен:
```
⚠️ Pattern (5× in 24h): Load spikes occur every ~30 minutes,
   suggesting a recurring cron job or scheduled batch process.
```
