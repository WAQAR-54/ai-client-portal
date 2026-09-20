/* Server Media (SuperAdmin): the "Delete file?" dialog and busy states.
 *
 * The dialog is a courtesy, never the protection - the server re-checks that no record refers to the
 * file before removing anything. This script only fills the dialog from data-* attributes (the name is
 * written with textContent, never as HTML) and shows progress while a request is in flight. */
(function () {
    "use strict";
    var modal = document.getElementById("mediaDeleteModal");

    function setBusy(button, label) {
        if (!button) { return; }
        button.dataset.idleLabel = button.textContent;
        button.textContent = label;
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
    }

    function resetBusy() {
        document.querySelectorAll("[data-idle-label]").forEach(function (button) {
            button.textContent = button.dataset.idleLabel;
            button.disabled = false;
            button.removeAttribute("aria-busy");
            delete button.dataset.idleLabel;
        });
        var wrap = document.getElementById("mediaTableWrap");
        if (wrap) { wrap.classList.remove("is-loading"); wrap.removeAttribute("aria-busy"); }
    }

    function closeModal() {
        if (modal) { modal.style.display = "none"; }
    }

    document.addEventListener("click", function (event) {
        var opener = event.target.closest("[data-media-delete]");
        if (opener && modal && !opener.disabled) {
            document.getElementById("mediaDeleteToken").value = opener.dataset.token || "";
            document.getElementById("mediaDeleteName").textContent = opener.dataset.name || "";
            modal.style.display = "flex";
            var cancel = document.getElementById("mediaDeleteCancel");
            if (cancel) { cancel.focus(); }  /* the safe choice is the default one */
            return;
        }
        if (event.target.closest("[data-media-cancel]")) {
            closeModal();
        }
        var link = event.target.closest("[data-media-busy-link]");
        if (link) {
            var wrap = document.getElementById("mediaTableWrap");
            if (wrap) { wrap.classList.add("is-loading"); wrap.setAttribute("aria-busy", "true"); }
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") { closeModal(); }
    });

    var deleteForm = document.getElementById("mediaDeleteForm");
    if (deleteForm) {
        deleteForm.addEventListener("submit", function () {
            var submit = document.getElementById("mediaDeleteSubmit");
            var cancel = document.getElementById("mediaDeleteCancel");
            setBusy(submit, submit.dataset.busyLabel || "…");
            if (cancel) { cancel.disabled = true; }
        });
    }

    document.querySelectorAll("form[data-media-busy]").forEach(function (form) {
        form.addEventListener("submit", function () {
            var button = form.querySelector("button[type=submit]");
            setBusy(button, form.dataset.busyLabel || "…");
            var wrap = document.getElementById("mediaTableWrap");
            if (wrap) { wrap.classList.add("is-loading"); wrap.setAttribute("aria-busy", "true"); }
        });
    });

    /* Coming back with the browser's Back button restores the page from memory: undo any busy state. */
    window.addEventListener("pageshow", function (event) {
        if (event.persisted) { resetBusy(); closeModal(); }
    });
})();

/* Bulk selection and bulk delete (SuperAdmin). The selection is only ever a list of opaque ids: the server decides,
 * per file and at the moment of deletion, whether it may be removed (still there, no record refers to it), and
 * reports exactly what it did. Nothing here can name a path. Deletion runs in small chunks so the page can show
 * real progress ("Deleting 20 of 40…") and a request never carries an unbounded batch. */
(function () {
    "use strict";
    var bar = document.getElementById("mediaBulkBar");
    var modal = document.getElementById("mediaBulkModal");
    if (!bar || !modal) { return; }

    var rows = Array.prototype.slice.call(document.querySelectorAll("input.media-select"));
    var pageBox = document.getElementById("mediaSelectPage");
    var countEl = document.getElementById("mediaBulkCount");
    var allButton = document.getElementById("mediaSelectAllFiltered");
    var deleteButton = document.getElementById("mediaBulkDelete");
    var chunkSize = parseInt(modal.dataset.chunk, 10) || 10;
    var maxPerRun = parseInt(modal.dataset.max, 10) || 100;
    var pageTotal = parseInt(bar.dataset.total, 10) || rows.length;
    var mode = "page";      /* "page": the ticked rows; "filtered": everything the server says matches the filters */
    var filtered = null;
    var busy = false;
    var finished = false;   /* a result is on screen: closing reloads the list so it reflects what happened */
    var plan = null;

    rows.forEach(function (box) { box.hidden = false; });
    if (pageBox) { pageBox.hidden = false; }

    function t(key, vars) {
        var text = (bar.dataset[key] || modal.dataset[key]) || "";
        Object.keys(vars || {}).forEach(function (name) { text = text.split("{" + name + "}").join(String(vars[name])); });
        return text;
    }
    function ticked() { return rows.filter(function (box) { return box.checked; }); }
    function bytes(n) {
        if (n < 1024) { return n + " B"; }
        if (n < 1048576) { return (n / 1024).toFixed(1) + " KB"; }
        if (n < 1073741824) { return (n / 1048576).toFixed(1) + " MB"; }
        return (n / 1073741824).toFixed(2) + " GB";
    }

    function refresh() {
        var picked = ticked();
        var n = mode === "filtered" ? filtered.total : picked.length;
        bar.hidden = n === 0;
        countEl.textContent = t("tCount", { n: n });
        if (pageBox) {
            pageBox.checked = picked.length === rows.length && rows.length > 0;
            pageBox.indeterminate = picked.length > 0 && picked.length < rows.length;
        }
        allButton.hidden = !(mode === "page" && picked.length === rows.length && pageTotal > rows.length);
    }
    function leaveFilteredMode() { mode = "page"; filtered = null; }

    rows.forEach(function (box) {
        box.addEventListener("change", function () { leaveFilteredMode(); refresh(); });
    });
    if (pageBox) {
        pageBox.addEventListener("change", function () {
            leaveFilteredMode();
            rows.forEach(function (box) { box.checked = pageBox.checked; });
            refresh();
        });
    }
    document.getElementById("mediaClearSelection").addEventListener("click", function () {
        leaveFilteredMode();
        rows.forEach(function (box) { box.checked = false; });
        refresh();
    });
    allButton.addEventListener("click", function () {
        allButton.disabled = true;
        fetch(bar.dataset.selectableUrl, { credentials: "same-origin", headers: { "Accept": "application/json" } })
            .then(function (response) { return response.ok ? response.json() : Promise.reject(); })
            .then(function (data) {
                filtered = data;
                mode = "filtered";
                rows.forEach(function (box) { box.checked = true; });
                refresh();
            })
            .catch(function () { window.alert(modal.dataset.tNetwork); })
            .then(function () { allButton.disabled = false; });
    });

    /* ---- the dialog ------------------------------------------------------------------------------------ */
    var panels = {
        confirm: document.getElementById("mediaBulkConfirm"),
        progress: document.getElementById("mediaBulkProgress"),
        result: document.getElementById("mediaBulkResult")
    };
    var confirmButton = document.getElementById("mediaBulkConfirmButton");
    var cancelButton = document.getElementById("mediaBulkCancel");
    var closeButton = document.getElementById("mediaBulkClose");

    function show(name) {
        Object.keys(panels).forEach(function (key) { panels[key].hidden = key !== name; });
        confirmButton.hidden = name !== "confirm";
        cancelButton.hidden = name !== "confirm";
        closeButton.hidden = name === "confirm";
        closeButton.disabled = name === "progress";
    }
    function line(text) {
        var item = document.createElement("li");
        item.textContent = text;
        return item;
    }
    function makePlan() {
        var picked = ticked();
        var selected, ids, eligibleTotal, referenced;
        if (mode === "filtered") {
            selected = filtered.total;
            ids = filtered.ids.slice();
            eligibleTotal = filtered.eligible;
            referenced = filtered.referenced;
        } else {
            selected = picked.length;
            ids = picked.filter(function (box) { return box.dataset.eligible === "1"; }).map(function (box) { return box.value; });
            eligibleTotal = ids.length;
            referenced = selected - ids.length;
        }
        var toDelete = ids.slice(0, maxPerRun);
        return { selected: selected, ids: toDelete, eligibleTotal: eligibleTotal, referenced: referenced, capped: eligibleTotal > toDelete.length };
    }
    function openBulk() {
        if (busy) { return; }
        plan = makePlan();
        finished = false;
        confirmButton.disabled = false;
        show("confirm");
        document.getElementById("mediaBulkSummary").textContent = plan.selected === 1 ? t("tSelectedOne") : t("tSelected", { n: plan.selected });
        var lines = document.getElementById("mediaBulkLines");
        lines.textContent = "";
        if (plan.referenced > 0) { lines.appendChild(line(plan.referenced === 1 ? t("tReferencedOne") : t("tReferenced", { n: plan.referenced }))); }
        if (plan.ids.length > 0) {
            lines.appendChild(line(plan.ids.length === 1 ? t("tEligibleOne") : t("tEligible", { n: plan.ids.length })));
            if (plan.capped) { lines.appendChild(line(t("tCapped", { n: plan.ids.length }))); }
            confirmButton.hidden = false;
            confirmButton.textContent = plan.ids.length === 1 ? t("tDeleteOne") : t("tDelete", { n: plan.ids.length });
        } else {
            lines.appendChild(line(t("tNone")));
            confirmButton.hidden = true;
        }
        modal.style.display = "flex";
        cancelButton.focus();  /* the safe choice is the default one */
    }
    function closeBulk() {
        if (busy) { return; }
        modal.style.display = "none";
        if (finished) { window.location.reload(); }
    }

    deleteButton.addEventListener("click", openBulk);
    modal.addEventListener("click", function (event) {
        if (event.target.closest("[data-bulk-cancel]") || event.target === closeButton) { closeBulk(); }
    });
    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && modal.style.display === "flex") { closeBulk(); }
    });
    window.addEventListener("beforeunload", function (event) {
        if (busy) { event.preventDefault(); event.returnValue = ""; }
    });

    function operationId() {
        if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
        return "op-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);
    }
    function send(ids, index, op, token) {
        var body = new FormData();
        ids.forEach(function (id) { body.append("ids", id); });
        body.append("op", op);
        body.append("chunk", String(index));
        return fetch(modal.dataset.url, {
            method: "POST", body: body, credentials: "same-origin", redirect: "manual",
            headers: { "X-CSRFToken": token, "Accept": "application/json" }
        }).then(function (response) {
            return response.json().then(
                function (json) { return { status: response.status, json: json }; },
                function () { return { status: response.status, json: null }; }
            );
        });
    }

    confirmButton.addEventListener("click", function () {
        if (busy || plan.ids.length === 0) { return; }
        busy = true;                       /* a second click, or Enter, while a run is active does nothing */
        confirmButton.disabled = true;
        show("progress");
        var total = plan.ids.length;
        var done = 0;
        var op = operationId();
        var token = modal.querySelector("input[name=csrfmiddlewaretoken]").value;
        var totals = { deleted: 0, skipped_referenced: plan.referenced, skipped_missing: 0, failed: 0, freed_bytes: 0 };
        var chunks = [];
        for (var i = 0; i < total; i += chunkSize) { chunks.push(plan.ids.slice(i, i + chunkSize)); }
        var meter = document.getElementById("mediaBulkProgressBar");
        var text = document.getElementById("mediaBulkProgressText");
        meter.max = total;
        function progress() { meter.value = done; text.textContent = t("tProgress", { done: done, total: total }); }
        progress();

        function finish(errorText) {
            busy = false;
            finished = true;
            show("result");
            document.getElementById("mediaBulkResultTitle").textContent = plan.selected === 1 ? t("tSelectedOne") : t("tSelected", { n: plan.selected });
            var list = document.getElementById("mediaBulkResultList");
            list.textContent = "";
            [
                [modal.dataset.labelDeleted, String(totals.deleted)],
                [modal.dataset.labelFreed, bytes(totals.freed_bytes)],
                [modal.dataset.labelReferenced, String(totals.skipped_referenced)],
                [modal.dataset.labelMissing, String(totals.skipped_missing)],
                [modal.dataset.labelFailed, String(totals.failed)]
            ].forEach(function (pair) {
                var wrap = document.createElement("div");
                var dt = document.createElement("dt"); dt.textContent = pair[0];
                var dd = document.createElement("dd"); dd.textContent = pair[1];
                wrap.appendChild(dt); wrap.appendChild(dd); list.appendChild(wrap);
            });
            var errorEl = document.getElementById("mediaBulkResultError");
            errorEl.hidden = !errorText;
            errorEl.textContent = errorText || "";
            closeButton.disabled = false;
            closeButton.focus();
        }

        (function next(index) {
            if (index >= chunks.length) { finish(""); return; }
            send(chunks[index], index, op, token).then(function (reply) {
                if (reply.status === 200 && reply.json && reply.json.ok) {
                    totals.deleted += reply.json.deleted;
                    totals.skipped_referenced += reply.json.skipped_referenced;
                    totals.skipped_missing += reply.json.skipped_missing;
                    totals.failed += reply.json.failed;
                    totals.freed_bytes += reply.json.freed_bytes;
                    done += chunks[index].length;
                    progress();
                    next(index + 1);
                } else {
                    var reason = (reply.json && reply.json.error) || modal.dataset.tNetwork;
                    finish(t("tStopped", { done: done, total: total, error: reason }));
                }
            }).catch(function () {
                finish(t("tStopped", { done: done, total: total, error: modal.dataset.tNetwork }));
            });
        })(0);
    });

    refresh();
})();
