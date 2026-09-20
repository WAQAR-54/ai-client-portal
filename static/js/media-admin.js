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
