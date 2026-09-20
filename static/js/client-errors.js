/* Reports browser-side failures to /client-errors/ (config/client_errors.py).
 *
 * It only LISTENS: it never cancels, swallows or rewrites an error, so the console, htmx's own
 * handling and the page's error messages behave exactly as before. What it sends is a kind, an HTTP
 * status and two URL paths - never a message, stack, form value or query string; the server scrubs
 * ids and tokens out of the paths. At most 20 reports per page load, so a reconnect loop cannot
 * flood the endpoint. */
(function () {
    "use strict";
    var ENDPOINT = "/client-errors/";
    var MAX_REPORTS = 20;
    var sent = 0;

    function report(kind, status, requestPath) {
        if (sent >= MAX_REPORTS) { return; }
        sent += 1;
        try {
            var body = JSON.stringify({
                kind: kind,
                status: typeof status === "number" ? status : 0,
                page: window.location.pathname,
                request: requestPath || ""
            });
            if (navigator.sendBeacon) {
                navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
            } else if (window.fetch) {
                window.fetch(ENDPOINT, { method: "POST", body: body, headers: { "Content-Type": "application/json" }, keepalive: true });
            }
        } catch (ignored) { /* reporting must never cause an error of its own */ }
    }

    function requestPathOf(event) {
        var detail = event && event.detail;
        var info = detail && (detail.pathInfo || {});
        return (info && (info.requestPath || info.finalRequestPath)) || (detail && detail.pathInfo && detail.pathInfo.path) || "";
    }

    document.addEventListener("htmx:responseError", function (event) {
        var xhr = event.detail && event.detail.xhr;
        report("htmx_response", xhr ? xhr.status : 0, requestPathOf(event));
    });
    document.addEventListener("htmx:sendError", function (event) {
        report("htmx_send", 0, requestPathOf(event));
    });
    document.addEventListener("htmx:sseError", function (event) {
        var source = event.detail && event.detail.source;
        report("sse", 0, source && source.url ? new URL(source.url, window.location.href).pathname : "");
    });
    window.addEventListener("error", function (event) {
        /* A failed <img>/<script> load fires on the element and does not bubble; only real script errors reach here. */
        if (event && event.message) { report("js_error", 0, ""); }
    });
    window.addEventListener("unhandledrejection", function () {
        report("promise_rejection", 0, "");
    });
})();
