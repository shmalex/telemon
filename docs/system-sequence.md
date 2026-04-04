# Telemon — полная диаграмма последовательности

```mermaid
sequenceDiagram
    actor User as 👤 Пользователь
    participant TG as 📱 Telegram
    participant telemon as telemon.py<br/>(main loop)
    participant chronic as chronic_tracker.py
    participant graph as LangGraph<br/>(diagnostics.py)
    participant llm as 🤖 LLM<br/>(OpenAI / Anthropic)
    participant llmlog as llm.log
    participant history as alert_history.json
    participant chronic_state as chronic_state.json
    participant chatbot as chatbot.py<br/>(daemon thread)
    participant system as 🖥️ Linux<br/>(psutil / journalctl / docker)

    %% ══════════════════════════════════════════════
    Note over telemon: Запуск сервиса (systemctl start)
    %% ══════════════════════════════════════════════

    telemon->>chatbot: start_chatbot_thread()
    telemon->>chronic_state: load chronic state
    telemon->>TG: 🚀 System monitor started<br/>Chatbot: ✅ enabled (gpt-4o-mini)

    %% ══════════════════════════════════════════════
    Note over telemon: Основной цикл (каждые 10 сек)
    %% ══════════════════════════════════════════════

    loop каждые 10 секунд

        telemon->>system: проверка CPU / RAM / Disk / Load / Swap
        telemon->>system: journalctl -p 3 (ошибки)
        telemon->>system: systemctl is-active (сервисы)
        telemon->>system: docker inspect (контейнеры)
        telemon->>system: pm2 jlist (процессы)
        telemon->>system: journalctl sshd (SSH логины)

        alt обычный алерт (disk, memory, cpu, swap)
            telemon->>TG: ⚠️ алерт
        end

        alt алерт load или disk_io
            telemon->>chronic: record(alert_type, peak_label)
            chronic->>chronic_state: загрузить / обновить счётчик

            alt статус = ACUTE (< 5 событий в окне)
                chronic-->>telemon: "acute"
                telemon->>graph: graph.invoke(alert_text)

                graph->>history: load_history — загрузить историю

                graph->>system: collect_context<br/>(CPU/RAM/Disk snapshot)

                loop до 4 итераций (без повторов)
                    graph->>llm: decide — что собрать дальше?
                    llm-->>llmlog: REQUEST/RESPONSE записан
                    llm-->>graph: "gather_processes" / "gather_disk_detail" /<br/>"gather_journal" / "gather_docker" / "done"

                    alt не "done"
                        graph->>system: собрать запрошенные данные
                        graph-->>graph: gathered += [(label, data)]
                    end
                end

                graph->>llm: detect_pattern — есть паттерн в истории?
                llm-->>llmlog: REQUEST/RESPONSE записан
                llm-->>graph: pattern_note (или пусто)

                graph->>llm: analyze — root cause?
                llm-->>llmlog: REQUEST/RESPONSE записан
                llm-->>graph: analysis text

                graph->>graph: format_report
                graph->>history: save_history — сохранить алерт

                graph-->>telemon: 🔍 Diagnostic report
                telemon->>TG: полный отчёт с анализом

            else статус = CHRONIC (5+ событий)
                chronic-->>telemon: "chronic"

                alt только что стал chronic (переход)
                    chronic-->>telemon: transition message
                    telemon->>TG: ⚠️ Chronic issue detected: disk_io<br/>Переведено в режим мониторинга
                end

                Note over telemon: полный отчёт подавлен
            end
        end

        alt journal error
            telemon->>TG: 🛑 System error [unit] message
        end

        alt SSH логин с неизвестного IP
            telemon->>TG: 🔐 Unknown SSH login!<br/>User / IP / Method / Time
        end

        alt сервис или контейнер упал
            telemon->>TG: 🚨 Service DOWN / 🐳 Container DOWN
        end

        alt сервис или контейнер восстановился
            telemon->>TG: ✅ Recovered — was down ~Xmin
        end

        %% --- Chronic hourly summary ---
        chronic->>chronic_state: проверить all chronic types

        alt chronic тип — пора слать summary (прошёл 1ч)
            chronic-->>telemon: hourly summary message
            telemon->>TG: 📋 Chronic: disk_io<br/>Last hour: 4 | Today: 18 | Peak: 536 MB/s
        end

        alt chronic тип — тишина 6ч → resolved
            chronic-->>telemon: resolved message
            chronic->>chronic_state: сбросить в ACUTE
            telemon->>TG: ✅ Chronic issue resolved: disk_io<br/>Lasted 4h 22min | Total: 23 events
        end

        %% --- Periodic digest ---
        alt прошло 8 часов
            telemon->>system: собрать 24h метрики из памяти
            telemon->>TG: 📊 System digest (24h)<br/>[PNG график Load + Disk I/O]
        end

    end

    %% ══════════════════════════════════════════════
    Note over chatbot: Chatbot polling loop (параллельный поток)
    %% ══════════════════════════════════════════════

    loop long-polling каждые 30 сек
        chatbot->>TG: getUpdates (offset=N)
        TG-->>chatbot: новые сообщения

        alt сообщение из CHATBOT_CHAT_ID
            User->>TG: "что с сервером?"
            chatbot->>llm: agent.invoke(question)
            llm-->>llmlog: REQUEST/RESPONSE записан

            loop пока LLM вызывает инструменты
                llm->>system: get_system_metrics /<br/>get_top_processes /<br/>get_recent_errors /<br/>get_docker_status /<br/>get_disk_io
                system-->>llm: данные
            end

            llm-->>chatbot: ответ
            chatbot->>TG: sendMessage(ответ)
            TG-->>User: 💬 ответ бота
        else сообщение из другого чата
            Note over chatbot: игнорируется (wrong chat_id)
        end
    end

    %% ══════════════════════════════════════════════
    Note over telemon: Остановка сервиса (SIGTERM)
    %% ══════════════════════════════════════════════

    telemon->>TG: 🛑 System monitor stopped.
```

---

## Файлы проекта

| Файл | Роль |
|---|---|
| `telemon.py` | главный цикл, все проверки, отправка алертов |
| `chatbot.py` | daemon thread, отвечает на вопросы в Telegram |
| `diagnostics.py` | LangGraph граф — итеративное расследование алерта |
| `chronic_tracker.py` | state machine ACUTE → CHRONIC → RESOLVED |
| `llm_logger.py` | LangChain callback — логирует все LLM вызовы |
| `alert_history.json` | история алертов для detect_pattern |
| `chronic_state.json` | персистентное состояние chronic tracker |
| `llm.log` | все промпты и ответы LLM verbatim |
