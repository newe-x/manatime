from datetime import date

from app.parser import extract_events, parse_description, parse_time_range

DAY = date(2026, 10, 4)


def test_time_ranges():
    assert parse_time_range("10:00 até 11:30") == ("10:00", "11:30")
    assert parse_time_range("10am to 11am") == ("10:00", "11:00")
    assert parse_time_range("11 to 12pm") == ("11:00", "12:00")
    assert parse_time_range("2 to 3:30pm") == ("14:00", "15:30")
    assert parse_time_range("10h às 11h30") == ("10:00", "11:30")
    assert parse_time_range("9:00 AM to 9:15 AM") == ("09:00", "09:15")
    assert parse_time_range("10 a 11") is None
    assert parse_time_range("4 de outubro de 2026") is None


def test_google_pt():
    ev = parse_description("10:00 até 11:00, Planejamento Q4, Joana Silva, Aceito, Local: Sala 3, 4 de outubro de 2026")
    assert ev["title"] == "Planejamento Q4"
    assert (ev["start"], ev["end"]) == ("10:00", "11:00")
    assert ev["response"] == "accepted" and ev["location"] == "Sala 3"


def test_google_en_tentative_and_allday():
    ev = parse_description("2pm to 3pm, 1:1 with Ana, Joana, Tentative, October 4, 2026")
    assert (ev["title"], ev["start"], ev["response"]) == ("1:1 with Ana", "14:00", "tentative")
    ev = parse_description("Dia inteiro, Feriado regional, Joana, 4 de outubro de 2026")
    assert ev["all_day"] and ev["title"] == "Feriado regional"


def test_google_declined_pt():
    ev = parse_description("12:00 até 13:00, Almoço com cliente, Joana, Recusado, domingo, 4 de outubro de 2026")
    assert ev["response"] == "declined" and ev["title"] == "Almoço com cliente"


def test_outlook_label():
    ev = parse_description("Revisão de contrato, 11:00 a 12:00, domingo, 4 de outubro de 2026, Local Teams, Organizador Ana Souza, Evento recorrente, Provisório")
    assert ev["title"] == "Revisão de contrato"
    assert (ev["start"], ev["end"]) == ("11:00", "12:00")
    assert ev["organizer"] == "Ana Souza" and ev["response"] == "tentative" and ev["location"] == "Teams"


def test_ignores_non_events():
    assert parse_description("Criar") is None
    assert parse_description("domingo, 4 de outubro de 2026") is None


def test_outlook_json_owa_and_graph():
    payload = [
        {"Body": {"Items": [
            {"ItemId": {"Id": "A1"}, "Subject": "Daily", "Start": "2026-10-04T09:00:00-03:00",
             "End": "2026-10-04T09:15:00-03:00", "Location": {"DisplayName": "Sala 1"},
             "Organizer": {"Mailbox": {"Name": "Ana"}}, "MyResponseType": "Accept",
             "OnlineMeetingJoinUrl": "https://teams.microsoft.com/l/meetup-join/x"},
            {"ItemId": {"Id": "A2"}, "Subject": "Amanhã", "Start": "2026-10-05T09:00:00-03:00",
             "End": "2026-10-05T10:00:00-03:00"},
            {"ItemId": {"Id": "A3"}, "Subject": "Cancelada", "IsCancelled": True,
             "Start": "2026-10-04T15:00:00-03:00", "End": "2026-10-04T16:00:00-03:00"},
        ]}},
        {"value": [{"id": "G1", "subject": "Sync", "isAllDay": False,
                    "start": {"dateTime": "2026-10-04T17:00:00.0000000", "timeZone": "UTC"},
                    "end": {"dateTime": "2026-10-04T18:00:00.0000000", "timeZone": "UTC"},
                    "responseStatus": {"response": "tentativelyAccepted"},
                    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/y"}},
                   {"id": "G2", "subject": "Feriado", "isAllDay": True,
                    "start": {"dateTime": "2026-10-04T00:00:00", "timeZone": "America/Sao_Paulo"},
                    "end": {"dateTime": "2026-10-05T00:00:00", "timeZone": "America/Sao_Paulo"}}]},
    ]
    from app.parser import find_outlook_events
    raw = []
    find_outlook_events(payload, raw)
    events, source = extract_events(DAY, {"json_seen": True, "json_events": raw, "descriptions": []})
    by_title = {e["title"]: e for e in events}
    assert source == "json"
    assert set(by_title) == {"Daily", "Sync", "Feriado"}
    assert by_title["Daily"]["meeting_url"].startswith("https://teams")
    assert (by_title["Daily"]["organizer"], by_title["Daily"]["response"]) == ("Ana", "accepted")
    assert (by_title["Sync"]["start"], by_title["Sync"]["response"]) == ("14:00", "tentative")
    assert by_title["Feriado"]["all_day"]


def test_dom_dedupe():
    d = "10:00 até 11:00, Daily, Aceito, 4 de outubro de 2026"
    events, source = extract_events(DAY, {"json_events": [], "descriptions": [d, d, "Daily"]})
    assert source == "dom" and len(events) == 1


def test_outlook_real_label_format():
    # Formato real do Outlook em pt-BR (com status em inglês), visto numa conta real
    d = ("[Time Azul] Daily Meeting, 09:00 para 09:30, Segunda-feira, 05 de Outubro de 2026, "
         "Reuniões do Microsoft Teams, Por Marina Costa, Busy, Evento recorrente")
    ev = parse_description(d)
    assert ev["title"] == "[Time Azul] Daily Meeting"
    assert (ev["start"], ev["end"]) == ("09:00", "09:30")
    assert ev["organizer"] == "Marina Costa"
    assert ev["date"] == date(2026, 10, 5)


def test_dom_filters_other_days_and_working_hours():
    descriptions = [
        "Daily, 09:00 para 09:30, Domingo, 04 de Outubro de 2026, Busy",
        "Planning, 10:00 para 10:30, Segunda-feira, 05 de Outubro de 2026, Tentative",
        "00:00 a 00:30, Domingo, 04 de Outubro de 2026, Fora do horário de trabalho",
    ]
    events, _ = extract_events(DAY, {"descriptions": descriptions})
    assert [e["title"] for e in events] == ["Daily"]


def test_outlook_json_skips_working_hours_and_merges_duplicates():
    from app.parser import find_outlook_events
    raw = []
    find_outlook_events([
        {"ItemId": {"Id": "wh"}, "Subject": "", "Start": "2026-10-04T08:00:00-03:00", "End": "2026-10-04T17:00:00-03:00"},
        {"ItemId": {"Id": "d"}, "Subject": "Daily", "Start": "2026-10-04T09:00:00-03:00", "End": "2026-10-04T09:30:00-03:00",
         "Organizer": {"Mailbox": {"Name": "Marina"}}},
        {"ItemId": {"Id": "d"}, "Subject": "Daily", "Start": "2026-10-04T09:00:00-03:00", "End": "2026-10-04T09:30:00-03:00",
         "OnlineMeetingJoinUrl": "https://teams.microsoft.com/l/z"},
        {"ItemId": {"Id": "r"}, "Subject": "Retrô", "Start": "2026-10-04T10:00:00-03:00", "End": "2026-10-04T11:30:00-03:00",
         "Organizer": {"Mailbox": {"Name": "Paula"}}, "Location": {"DisplayName": "https://meet.google.com/abc-defg-hij"}},
    ], raw)
    events, source = extract_events(DAY, {"json_seen": True, "json_events": raw, "descriptions": []})
    by_title = {e["title"]: e for e in events}
    assert set(by_title) == {"Daily", "Retrô"}
    assert by_title["Daily"]["meeting_url"] == "https://teams.microsoft.com/l/z"
    assert by_title["Retrô"]["meeting_url"] == "https://meet.google.com/abc-defg-hij"


def test_json_seen_but_empty_day_does_not_fall_back_to_dom():
    d = "Daily, 09:00 para 09:30, Domingo, 04 de Outubro de 2026"
    events, source = extract_events(DAY, {"json_seen": True, "json_events": [], "descriptions": [d]})
    assert source == "json" and events == []


def test_google_real_format():
    ev = parse_description("15:00 - 15:30, [APRESENTAÇÃO] Projeto Atlas, Joana Silva, Aceito, Nenhum local, 28 de setembro de 2026")
    assert (ev["title"], ev["start"], ev["end"], ev["location"]) == ("[APRESENTAÇÃO] Projeto Atlas", "15:00", "15:30", None)
    assert ev["response"] == "accepted" and ev["date"] == date(2026, 9, 28)
    ev = parse_description("Dia inteiro, Nossa Senhora de Aparecida, Agenda: Feriados no Brasil, 12 de outubro de 2026")
    assert ev["all_day"] and ev["title"] == "Nossa Senhora de Aparecida"


def test_google_expected_count():
    from app.parser import expected_count
    descs = ["segunda-feira, 28 de setembro de 2026, 2 eventos", "segunda-feira, 28 de setembro",
             "domingo, 27 de setembro de 2026, 5 eventos"]
    assert expected_count(descs, date(2026, 9, 28)) == 2
    assert expected_count(["segunda-feira, 5 de outubro de 2026"], date(2026, 10, 5)) is None


def test_google_week_page_splits_days_and_checks_total():
    from app.parser import extract_range
    page = {"start": date(2026, 9, 27), "end": date(2026, 10, 4), "descriptions": [
        "Semana de 27 de setembro de 2026, 3 eventos",
        "15:00 - 15:30, [APRESENTAÇÃO] Projeto Atlas, Joana Silva, Aceito, Nenhum local, 28 de setembro de 2026",
        "09:00 - 09:30, Apresentação Projeto Atlas, Joana Silva, Aceito, Nenhum local, 29 de setembro de 2026",
        "16:00 - 16:15, APRESENTAÇÃO COMERCIAL, Joana Silva, Aceito, Nenhum local, 29 de setembro de 2026",
        "segunda-feira, 28 de setembro",
    ]}
    by_day, missing, source = extract_range(date(2026, 9, 27), date(2026, 10, 4), {"pages": [page]})
    assert source == "dom" and missing == {}
    assert [e["title"] for e in by_day[date(2026, 9, 28)]] == ["[APRESENTAÇÃO] Projeto Atlas"]
    assert len(by_day[date(2026, 9, 29)]) == 2 and by_day[date(2026, 9, 30)] == []

    page["descriptions"][0] = "Semana de 27 de setembro de 2026, 4 eventos"
    _, missing, _ = extract_range(date(2026, 9, 27), date(2026, 10, 4), {"pages": [page]})
    assert missing[date(2026, 9, 28)] == 1 and len(missing) == 7


def test_forwarded_invite_is_merged():
    from app.parser import merge_forwarded
    base = {"start": "10:00", "end": "11:30", "all_day": False, "location": None, "meeting_url": None, "organizer": "Paula"}
    merged = merge_forwarded([
        {**base, "title": "ENC: [Time Azul] Retrô", "response": "accepted"},
        {**base, "title": "[Time Azul] Retrô", "response": "unknown"},
        {**base, "title": "Outra", "response": "accepted"},
    ])
    titles = sorted(e["title"] for e in merged)
    assert titles == ["Outra", "[Time Azul] Retrô"]
    assert next(e for e in merged if e["title"] == "[Time Azul] Retrô")["response"] == "accepted"
