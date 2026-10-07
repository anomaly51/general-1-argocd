"""Downloader log enrichment and one native OpenSearch dashboard (Global tenant)."""

import json

PIPELINE_ID = "downloader-logs-v1"
BOT_FILTER = [
    {"term": {"resource.attributes.k8s.namespace.name": "apps"}},
    {"term": {"resource.attributes.k8s.deployment.name": "downloader-sloniara-bot"}},
]

# Only controlled labels are copied to the overview, not chat IDs, URLs or prompts.
PROXY_EVENTS = {
    "start": ["Proxy", "Proxy download started", "started"],
    "sent_start": ["Proxy", "Start command sent", "progress"],
    "start_response": ["Proxy", "Start response received", "progress"],
    "start_response_timeout": ["Proxy", "Start response timed out; URL may still be sent", "warning"],
    "sent_url": ["Proxy", "URL sent to proxy", "progress"],
    "response": ["Proxy", "Proxy response received", "progress"],
    "response_timeout": ["Proxy", "Proxy response timed out", "error"],
    "proxy_error_message": ["Proxy", "Proxy returned an error", "error"],
    "text_link_response_ignored": ["Proxy", "Text-only response ignored", "progress"],
    "max_messages_without_media": ["Proxy", "Response limit reached without media", "error"],
    "exception": ["Proxy", "Proxy download exception", "error"],
    "telegram_media_ok": ["Proxy", "Proxy media available for delivery", "success"],
    "download_media_empty": ["Proxy", "Proxy media download was empty", "error"],
    "download_media_ok": ["Proxy", "Proxy media downloaded locally", "success"],
    "photo_album_video_ok": ["Proxy", "Proxy photo album converted to video", "success"],
    "unknown_join_target": ["Subscription", "Unknown subscription target", "error"],
    "sponsor_join_ok": ["Subscription", "Required channel joined", "success"],
    "sponsor_already_joined": ["Subscription", "Already subscribed", "progress"],
    "sponsor_join_failed": ["Subscription", "Channel subscription failed", "error"],
    "check_subscription_clicked": ["Subscription", "Subscription check requested", "progress"],
    "check_subscription_click_failed": ["Subscription", "Subscription check failed", "error"],
    "check_subscription_button_not_found": ["Subscription", "Subscription check button missing", "warning"],
    "subscription_required_auto_join_disabled": ["Subscription", "Subscription required; auto-join disabled", "error"],
    "subscription_required_auto_join_start": ["Subscription", "Automatic subscription started", "started"],
    "subscription_required_no_telegram_targets": ["Subscription", "No supported subscription targets", "error"],
    "subscription_required_auto_join_done": ["Subscription", "Automatic subscription finished", "progress"],
    "resent_url_after_subscription": ["Subscription", "URL resent after subscription", "progress"],
    "youtube_quality_clicked": ["Quality", "YouTube quality selected", "success"],
    "youtube_quality_click_failed": ["Quality", "YouTube quality selection failed", "error"],
}

PARSER = r'''
double numberAfter(String text, String marker) {
    int start = text.indexOf(marker);
    if (start < 0) return -1;
    start += marker.length();
    int end = start;
    while (end < text.length()) {
        char ch = text.charAt(end);
        if ((ch >= 48 && ch <= 57) || ch == 46) end++; else break;
    }
    if (end == start) return -1;
    try { return Double.parseDouble(text.substring(start, end)); }
    catch (Exception e) { return -1; }
}
def attrs = ctx.resource?.attributes;
if (attrs == null || attrs['k8s.namespace.name'] != 'apps' ||
    attrs['k8s.deployment.name'] != 'downloader-sloniara-bot' || !(ctx.body instanceof String)) return;
String b = ctx.body;
def d = ['parser_version': 1, 'event': 'application_message', 'stage': 'Other',
         'summary': 'Application message', 'outcome': 'progress', 'level': 'INFO'];
ctx.downloader = d;
int prefix = b.indexOf(' INFO [');
if (prefix < 0) { prefix = b.indexOf(' ERROR ['); if (prefix >= 0) d.level = 'ERROR'; }
if (prefix >= 0) {
    int start = b.indexOf('[', prefix);
    int end = b.indexOf(']', start);
    int payload = b.indexOf(' {', end);
    if (end > start && payload > end) {
        d.request_id = b.substring(start + 1, end);
        d.event = b.substring(end + 2, payload);
        def label = params.proxy_events[d.event];
        if (label != null) { d.stage = label[0]; d.summary = label[1]; d.outcome = label[2]; }
        ctx.downloader_payload = b.substring(payload + 1);
    }
} else if (b.startsWith('Сообщение со ссылкой:')) {
    d.event = 'link_received'; d.stage = 'Intake'; d.summary = 'Message with a supported link received';
} else if (b.startsWith('Контент отправлен в чат')) {
    d.event = 'content_sent'; d.stage = 'Delivery'; d.summary = 'Content sent to Telegram'; d.outcome = 'success';
} else if (b.startsWith('Отправляю ')) {
    d.event = 'send_started'; d.stage = 'Delivery'; d.summary = 'Sending media to Telegram';
    d.content_type = b.substring(10, b.indexOf(' в чат'));
} else if (b.startsWith('gallery-dl:')) {
    d.event = 'direct_download_completed'; d.stage = 'Direct download';
    d.summary = 'Direct media download completed'; d.outcome = 'success';
    double seconds = numberAfter(b, 'gallery-dl: ');
    if (seconds >= 0) d.duration_seconds = seconds;
    for (String key : ['photos', 'videos', 'audio']) {
        double value = numberAfter(b, key + '='); if (value >= 0) d[key] = (int)value;
    }
} else if (b.startsWith('slideshow:')) {
    d.event = 'slideshow_completed'; d.stage = 'Slideshow';
    d.summary = 'Photo slideshow encoded'; d.outcome = 'success';
    double seconds = numberAfter(b, 'total='); if (seconds >= 0) d.duration_seconds = seconds;
    for (String key : ['prepare', 'encode']) {
        double value = numberAfter(b, key + '='); if (value >= 0) d[key + '_seconds'] = value;
    }
} else if (b.startsWith('Ошибка gallery-dl:') || b.startsWith('Неожиданная ошибка gallery-dl:') ||
           b.startsWith('gallery-dl превысил') || b.startsWith('gallery-dl не вернул')) {
    d.event = 'direct_download_failed'; d.stage = 'Direct download';
    d.summary = 'Direct download failed; proxy fallback may recover'; d.outcome = 'error';
} else if (b.startsWith('Инструкция:')) {
    d.event = 'instruction_received'; d.stage = 'Intake'; d.summary = 'User instruction received';
} else if (b.startsWith('Команда конвертации:')) {
    d.event = 'conversion_selected'; d.stage = 'Conversion'; d.summary = 'Conversion preference evaluated';
    if (b.endsWith('mp3')) d.content_type = 'audio';
    else if (b.endsWith('voice')) d.content_type = 'voice';
} else if (b.startsWith('readers: tracked')) {
    d.event = 'readers_tracked'; d.stage = 'Readers'; d.summary = 'Read tracking started';
} else if (b.startsWith('readers: updated')) {
    d.event = 'readers_updated'; d.stage = 'Readers'; d.summary = 'Read count updated';
    double value = numberAfter(b, 'readers='); if (value >= 0) d.readers = (int)value;
} else if (b.contains('Telegram health check failed:')) {
    d.event = 'telegram_health_failed'; d.stage = 'Connection'; d.summary = 'Telegram health check failed'; d.outcome = 'error';
} else if (b.contains('Server closed the connection:') || b.contains('Connection reset by peer')) {
    d.event = 'connection_lost'; d.stage = 'Connection'; d.summary = 'Telegram connection interrupted'; d.outcome = 'error';
} else if (b.startsWith('Ошибка при сокращении заголовка:') || b.startsWith('Ошибка генерации сообщения:')) {
    d.event = 'llm_failed'; d.stage = 'LLM'; d.summary = 'LLM request failed'; d.outcome = 'error';
} else if (b.startsWith('Ошибка при отправке контента:') || b.startsWith('Ошибка при редактировании сообщения:')) {
    d.event = 'delivery_failed'; d.stage = 'Delivery'; d.summary = 'Telegram send or edit failed'; d.outcome = 'error';
} else if (b.startsWith('Ошибка конвертации')) {
    d.event = 'conversion_failed'; d.stage = 'Conversion'; d.summary = 'FFmpeg conversion failed'; d.outcome = 'error';
} else if (b.startsWith('Ошибка при очистке файлов:')) {
    d.event = 'cleanup_failed'; d.stage = 'Cleanup'; d.summary = 'Temporary file cleanup failed'; d.outcome = 'error';
} else if (b.startsWith('Ошибка') && b.contains('readers')) {
    d.event = 'readers_failed'; d.stage = 'Readers'; d.summary = 'Read tracking failed'; d.outcome = 'error';
} else if (b.startsWith('Критическая ошибка:')) {
    d.event = 'unhandled_exception'; d.stage = 'Application'; d.summary = 'Unhandled processing exception'; d.outcome = 'error';
} else if (b.startsWith('Клиент запускается') || b.startsWith('Клиент запущен.')) {
    d.event = 'client_start'; d.stage = 'Startup'; d.summary = 'Telegram client starting or ready';
} else if (b.startsWith('Подробности fallback-прокси:')) {
    d.event = 'download_exhausted'; d.stage = 'Proxy'; d.summary = 'No media after direct and proxy attempts'; d.outcome = 'error';
}
if (d.outcome == 'error') d.level = 'ERROR';
else if (d.outcome == 'warning') d.level = 'WARN';
// Only classify source URLs on intake/start events, not inside arbitrary user text.
if (d.event == 'link_received' || d.event == 'start') {
    if (b.contains('tiktok.com/')) d.platform = 'TikTok';
    else if (b.contains('instagram.com/')) d.platform = 'Instagram';
    else if (b.contains('youtube.com/') || b.contains('youtu.be/')) d.platform = 'YouTube';
}
'''

PIPELINE = {
    "description": "Enrich only downloader bot logs; preserve raw records and other applications.",
    "processors": [
        {"script": {"lang": "painless", "source": PARSER, "params": {"proxy_events": PROXY_EVENTS}}},
        {"json": {"if": "ctx.containsKey('downloader_payload')", "field": "downloader_payload",
                  "target_field": "downloader_details", "ignore_failure": True}},
        {"script": {"if": "ctx.containsKey('downloader_details')", "source": """
            def p = ctx.downloader_details; def d = ctx.downloader;
            if (p instanceof Map) {
                if (['video', 'photos', 'audio', 'voice'].contains(p.content_type)) d.content_type = p.content_type;
                if (p.selected_quality instanceof String || p.selected_quality instanceof Number) d.quality = p.selected_quality.toString();
                if (p.photos instanceof Number) d.photos = p.photos;
            }
        """}},
        {"remove": {"field": ["downloader_payload", "downloader_details"], "ignore_missing": True}},
    ],
    # A malformed log must never stop ingestion. Failure is visible for diagnostics.
    "on_failure": [
        {"set": {"field": "downloader.parse_failed", "value": True}},
        {"remove": {"field": ["downloader_payload", "downloader_details"], "ignore_missing": True}},
    ],
}

MAPPING = {"properties": {"downloader": {"properties": {
    **{key: {"type": "keyword"} for key in
       ("event", "stage", "summary", "outcome", "level", "platform", "content_type", "request_id", "quality")},
    **{key: {"type": "float"} for key in ("duration_seconds", "prepare_seconds", "encode_seconds")},
    **{key: {"type": "integer"} for key in ("parser_version", "photos", "videos", "audio", "readers")},
    "parse_failed": {"type": "boolean"},
}}}}


def saved_objects():
    """Stable IDs let the existing PostSync setup job reconcile this dashboard."""
    objects, panels = [], []
    base_query = ('resource.attributes.k8s.namespace.name:"apps" AND '
                  'resource.attributes.k8s.deployment.name:"downloader-sloniara-bot"')
    index_ref = {"name": "kibanaSavedObjectMeta.searchSourceJSON.index", "type": "index-pattern", "id": "general1-logs"}

    def add(slug, title, kind, params, aggs, position, query=""):
        object_id = "downloader-" + slug
        for i, agg in enumerate(aggs):
            agg.update({"id": str(i + 1), "enabled": True})
        source = {"query": {"language": "kuery", "query": base_query + (" AND (" + query + ")" if query else "")},
                  "filter": [], "indexRefName": index_ref["name"]}
        attrs = {"title": title, "description": "Managed in general-1-argocd; downloader application logs only.",
                 "visState": json.dumps({"title": title, "type": kind, "params": params, "aggs": aggs}),
                 "uiStateJSON": "{}", "version": 1,
                 "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(source)}}
        objects.append({"type": "visualization", "id": object_id, "attributes": attrs, "references": [index_ref]})
        panel(object_id, "visualization", position)

    def panel(object_id, kind, position):
        idx = str(len(panels) + 1)
        x, y, w, h = position
        panels.append({"version": "3.9.0", "panelIndex": idx, "type": kind,
                       "panelRefName": "panel_" + idx, "embeddableConfig": {},
                       "gridData": {"x": x, "y": y, "w": w, "h": h, "i": idx}, "_id": object_id})

    def count(label="Events"):
        return {"type": "count", "schema": "metric", "params": {"customLabel": label}}

    def terms(field, schema="segment"):
        return {"type": "terms", "schema": schema,
                "params": {"field": "downloader." + field, "size": 12, "order": "desc", "orderBy": "1"}}

    add("notes", "Reading the overview", "markdown", {"markdown":
        "Errors count **failed steps**, not failed requests. Timings cover direct download and slideshow only. "
        "Source platforms are known only for proxy downloads.", "openLinksInNewTab": False}, [], (0, 0, 48, 4))
    for i, (slug, title, query) in enumerate([
        ("received", "Incoming messages", 'downloader.event:"link_received"'),
        ("sent", "Content sent", 'downloader.event:"content_sent"'),
        ("proxy-starts", "Proxy starts", 'downloader.event:"start"'),
        ("errors", "Error events", 'downloader.outcome:"error"'),
    ]):
        add(slug, title, "metric", {"metric": {"style": {"fontSize": 32}, "labels": {"show": False},
            "colorSchema": "Greens", "colorsRange": [{"from": 0, "to": 10000}], "invertColors": False,
            "percentageMode": False, "useRanges": False}}, [count()], (i * 12, 4, 12, 5), query)

    filters = {"type": "filters", "schema": "group", "params": {"filters": [
        {"input": {"query": 'downloader.event:"' + event + '"', "language": "kuery"}, "label": label}
        for event, label in [("link_received", "Incoming messages"), ("content_sent", "Content sent"), ("start", "Proxy starts")]
    ]}}
    add("activity", "Activity over time", "histogram", {"type": "histogram", "addLegend": True,
        "addTooltip": True, "legendPosition": "bottom", "times": [], "grid": {"categoryLines": False},
        "categoryAxes": [{"id": "CategoryAxis-1", "type": "category", "position": "bottom", "show": True,
                          "labels": {"show": True}, "title": {"text": "Time"}}],
        "valueAxes": [{"id": "ValueAxis-1", "type": "value", "position": "left", "show": True,
                       "scale": {"type": "linear"}, "labels": {"show": True}, "title": {"text": "Events"}}],
        "seriesParams": [{"show": True, "type": "histogram", "mode": "normal", "data": {"id": "1", "label": "Events"},
                          "valueAxis": "ValueAxis-1", "drawLinesBetweenPoints": True, "showCircles": False}]},
        [count(), {"type": "date_histogram", "schema": "segment", "params": {"field": "time", "interval": "auto",
                                                                                 "min_doc_count": 0}}, filters], (0, 9, 32, 11))
    pie = {"type": "pie", "isDonut": True, "addLegend": True, "addTooltip": True, "legendPosition": "bottom",
           "labels": {"show": False}}
    add("sources", "Proxy sources", "pie", pie, [count(), terms("platform")], (32, 9, 16, 11),
        'downloader.event:"start"')
    table = {"perPage": 10, "showPartialRows": False, "showMetricsAtAllLevels": False, "showTotal": False,
             "totalFunc": "sum"}
    add("errors-by-stage", "Errors by stage", "table", table,
        [count("Error events"), terms("stage", "bucket")], (0, 20, 24, 10), 'downloader.outcome:"error"')
    add("download-events", "Download outcomes · steps", "table", table,
        [count(), terms("summary", "bucket")], (24, 20, 24, 10),
        'downloader.event:("direct_download_completed" OR "direct_download_failed" OR "telegram_media_ok" OR '
        '"download_media_ok" OR "photo_album_video_ok" OR "response_timeout" OR "download_exhausted")')
    add("duration", "Stage duration (s)", "table", table,
        [count("Samples"), {"type": "avg", "schema": "metric", "params": {"field": "downloader.duration_seconds", "customLabel": "Average (s)"}},
         {"type": "max", "schema": "metric", "params": {"field": "downloader.duration_seconds", "customLabel": "Max (s)"}},
         terms("stage", "bucket")], (0, 30, 24, 9), 'downloader.duration_seconds:*')
    add("media", "Media types · prepared", "pie", pie,
        [count(), terms("content_type")], (24, 30, 24, 9),
        '(downloader.event:"send_started" AND NOT downloader.content_type:"telegram_media") OR downloader.event:"telegram_media_ok"')
    source = {"query": {"language": "kuery", "query": base_query + ' AND NOT downloader.event:"application_message"'},
              "filter": [], "indexRefName": index_ref["name"]}
    objects.append({"type": "search", "id": "downloader-recent-events", "references": [index_ref], "attributes": {
        "title": "Recent events · expand a row for raw log", "description": "Overview columns omit chat IDs, URLs and instructions.",
        "columns": ["downloader.stage", "downloader.summary", "downloader.level", "downloader.request_id"],
        "sort": [["time", "desc"]], "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(source)}}})
    panel("downloader-recent-events", "search", (0, 39, 48, 15))
    references = [{"name": p["panelRefName"], "type": p["type"], "id": p.pop("_id")} for p in panels]
    objects.append({"type": "dashboard", "id": "downloader-bot-overview", "references": references, "attributes": {
        "title": "Downloader Bot Overview", "description": "Activity, download paths, errors and recent Telegram bot events.",
        "panelsJSON": json.dumps(panels), "optionsJSON": json.dumps({"useMargins": True, "hidePanelTitles": False}),
        "timeRestore": True, "timeFrom": "now-24h", "timeTo": "now", "refreshInterval": {"pause": False, "value": 60000},
        "version": 1, "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps({"query": {"language": "kuery", "query": ""}, "filter": []})},
    }})
    return objects
