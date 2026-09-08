/* Shared vanilla-JS helpers for the admin redesign components: a generic
   modal shell and a sliding-indicator (filter chips / nav highlight)
   positioner. Kept as one small shared file - unlike the rest of this
   project's one-off inline <script> blocks, these three patterns are now
   used across 5+ templates, so duplicating them invited drift.

   Naming follows the existing portalOpenUpgradeModal()/portalCloseUpgradeModal()
   convention in templates/chat/chat_home.html (that one stays as-is, not
   migrated to this file) - these are the generic, ID-based versions for
   any NEW modal built on the .modal-overlay/.modal-box shell. */

function portalOpenModal(id) {
    var overlay = document.getElementById(id);
    if (overlay) overlay.style.display = "flex";
}

function portalCloseModal(id) {
    var overlay = document.getElementById(id);
    if (overlay) overlay.style.display = "none";
}

document.addEventListener("mousedown", function(evt) {
    if (evt.target.classList && evt.target.classList.contains("modal-overlay")) {
        portalCloseModal(evt.target.id);
    }
});

/* Positions `indicatorEl` (an absolutely-positioned sibling of `targetEl`)
   to sit behind targetEl, sized/placed via offsetLeft/offsetTop so it
   works for both a horizontal row (filter chips) and a vertical stack
   (nav items) - pass axis "x" or "y". No-ops quietly if either element is
   missing, so callers don't need their own existence checks. */
function portalMoveIndicator(indicatorEl, targetEl, axis) {
    if (!indicatorEl || !targetEl) return;
    if (axis === "y") {
        indicatorEl.style.transform = "translateY(" + targetEl.offsetTop + "px)";
        indicatorEl.style.height = targetEl.offsetHeight + "px";
    } else {
        indicatorEl.style.transform = "translateX(" + targetEl.offsetLeft + "px)";
        indicatorEl.style.width = targetEl.offsetWidth + "px";
    }
}

/* On load, position every [data-indicator-for] indicator behind whichever
   sibling currently carries the "active" class - handles the common case
   (a filter row or nav list rendered with its active item server-side)
   without each template needing its own bootstrap script. A page that
   re-renders part of this via htmx and wants the indicator to animate to
   a NEW active item calls portalMoveIndicator itself from an
   htmx:afterSwap listener (see templates/governance/users.html). */
document.addEventListener("DOMContentLoaded", function() {
    document.querySelectorAll("[data-indicator-for]").forEach(function(indicatorEl) {
        var containerSelector = indicatorEl.getAttribute("data-indicator-for");
        var container = document.querySelector(containerSelector);
        if (!container) return;
        var active = container.querySelector(".active");
        if (!active) return;
        var axis = indicatorEl.getAttribute("data-indicator-axis") || "x";
        portalMoveIndicator(indicatorEl, active, axis);
    });
});

/* Fades+rises a group of elements in on load, staggered - the same quiet
   entrance the Dashboard's KPI/chart cards already use, generalized so any
   page can opt in instead of every page needing its own copy of this. Skips
   entirely under prefers-reduced-motion. Add data-stagger to a container;
   data-stagger-items picks which of its descendants animate (defaults to
   direct children) - e.g. <div class="plan-grid" data-stagger
   data-stagger-items=".plan-card">, or a <tbody data-stagger> with rows
   picked up as its direct <tr> children automatically. */
function portalStaggerIn(container) {
    var itemsSelector = container.getAttribute("data-stagger-items");
    var items = itemsSelector ? container.querySelectorAll(itemsSelector) : container.children;
    Array.prototype.forEach.call(items, function(el, i) {
        el.style.opacity = "0";
        el.style.transform = "translateY(10px)";
        el.style.transition = "opacity .5s var(--ease), transform .5s var(--ease)";
        setTimeout(function() {
            el.style.opacity = "1";
            el.style.transform = "translateY(0)";
            // Clear the inline styles once settled - left in place they'd
            // permanently out-specificity any CSS :hover lift on these items.
            setTimeout(function() {
                el.style.opacity = "";
                el.style.transform = "";
                el.style.transition = "";
            }, 520);
        }, i * 45);
    });
}
function portalReduceMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
document.addEventListener("DOMContentLoaded", function() {
    if (portalReduceMotion()) return;
    document.querySelectorAll("[data-stagger]").forEach(portalStaggerIn);
});
// A page that re-renders a [data-stagger] container via htmx (e.g. the
// Users/Models tables after a filter) gets the same entrance again on each
// swap - reinforces "something changed" instead of the new rows just
// silently appearing.
document.addEventListener("htmx:afterSwap", function(evt) {
    if (portalReduceMotion()) return;
    var target = evt.detail && evt.detail.target;
    if (!target || !target.querySelectorAll) return;
    if (target.matches && target.matches("[data-stagger]")) portalStaggerIn(target);
    target.querySelectorAll("[data-stagger]").forEach(portalStaggerIn);
});

/* Quick light/dark toggle in the header (see .theme-toggle-quick in
   main.css) - one delegated listener so any number of these on a page work
   without a per-template inline script. Reads/writes the same data-theme
   attribute and accounts:set_theme_preference endpoint as the full picker
   on the Settings > Display tab, so a choice made here is exactly as
   persistent (saved to the user's own record, not just this tab). */
document.addEventListener("click", function(evt) {
    var btn = evt.target.closest(".theme-toggle-quick");
    if (!btn) return;
    var root = document.documentElement;
    var explicit = root.getAttribute("data-theme");
    var isDark = explicit === "dark" || (explicit !== "light" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
    var next = isDark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    var formData = new FormData();
    formData.append("theme", next);
    fetch(btn.getAttribute("data-set-theme-url"), {
        method: "POST",
        headers: {"X-CSRFToken": btn.getAttribute("data-csrf")},
        body: formData,
    });
});

/* Themed confirmation modal - replaces the browser's native confirm()
   popup everywhere in the app (both htmx's hx-confirm and plain-form
   onsubmit confirms), so a destructive action always gets the same
   in-app-styled dialog instead of an OS-chrome popup. Shared single
   instance (#portalConfirmModal) declared once in base.html. */
var _portalConfirmCallback = null;

function portalConfirm(message, onConfirm) {
    var messageEl = document.getElementById("portalConfirmMessage");
    if (messageEl) messageEl.textContent = message;
    _portalConfirmCallback = onConfirm;
    portalOpenModal("portalConfirmModal");
}

document.addEventListener("DOMContentLoaded", function() {
    var btn = document.getElementById("portalConfirmButton");
    if (!btn) return;
    btn.addEventListener("click", function() {
        var callback = _portalConfirmCallback;
        _portalConfirmCallback = null;
        portalCloseModal("portalConfirmModal");
        if (callback) callback();
    });
});

/* For a plain (non-htmx) form: onsubmit="return portalConfirmSubmit(event, '...')".
   First call intercepts and shows the modal; if confirmed, marks the form
   and re-submits (requestSubmit re-fires onsubmit, so the mark short-
   circuits straight through the second time instead of looping). */
function portalConfirmSubmit(evt, message) {
    var form = evt.target;
    if (form.dataset.portalConfirmed) {
        delete form.dataset.portalConfirmed;
        return true;
    }
    evt.preventDefault();
    portalConfirm(message, function() {
        form.dataset.portalConfirmed = "1";
        form.requestSubmit();
    });
    return false;
}

/* For every htmx-enhanced form using hx-confirm="...": htmx fires this
   event instead of calling window.confirm() itself when a listener calls
   preventDefault() - covers every hx-confirm attribute site-wide from this
   one listener, no per-template changes needed. Bound to `document`, not
   document.body - this script tag sits in <head> and runs before <body>
   exists, so document.body is still null at this point (document.body.
   addEventListener would throw silently and never attach). htmx events
   bubble all the way to `document` just as well. */
document.addEventListener("htmx:confirm", function(evt) {
    if (!evt.detail.question) return;
    evt.preventDefault();
    portalConfirm(evt.detail.question, function() {
        evt.detail.issueRequest(true);
    });
});

/* Smooth open/close for any <details class="smooth-details"> (e.g. a
   Provider card's "Manage models" panel) - plain CSS can't animate to/from
   "auto" height, so this measures the real content height and animates
   with the Web Animations API instead (broadly supported for years,
   unlike the still-patchy CSS calc-size()/allow-keywords route). Standard
   pattern - see web.dev's "Building an expand and collapse component". */
function portalSmoothDetails(details) {
    if (details._portalSmoothBound) return;
    details._portalSmoothBound = true;
    var summary = details.querySelector("summary");
    if (!summary) return;
    var animation = null;
    var isClosing = false;
    var isExpanding = false;

    summary.addEventListener("click", function(e) {
        e.preventDefault();
        details.style.overflow = "hidden";
        if (isClosing || !details.open) {
            openAndExpand();
        } else if (isExpanding || details.open) {
            shrink();
        }
    });

    function shrink() {
        isClosing = true;
        var startHeight = details.offsetHeight + "px";
        var endHeight = summary.offsetHeight + "px";
        if (animation) animation.cancel();
        animation = details.animate(
            { height: [startHeight, endHeight] },
            { duration: portalReduceMotion() ? 0 : 200, easing: "ease-out" }
        );
        animation.onfinish = function() { onAnimationFinish(false); };
        animation.oncancel = function() { isClosing = false; };
    }

    function openAndExpand() {
        details.style.height = details.offsetHeight + "px";
        details.open = true;
        window.requestAnimationFrame(function() { expand(); });
    }

    function expand() {
        isExpanding = true;
        var startHeight = details.offsetHeight + "px";
        var endHeight = summary.offsetHeight + Array.from(details.children).reduce(function(sum, child) {
            return child === summary ? sum : sum + child.offsetHeight;
        }, 0);
        endHeight += "px";
        if (animation) animation.cancel();
        animation = details.animate(
            { height: [startHeight, endHeight] },
            { duration: portalReduceMotion() ? 0 : 200, easing: "ease-out" }
        );
        animation.onfinish = function() { onAnimationFinish(true); };
        animation.oncancel = function() { isExpanding = false; };
    }

    function onAnimationFinish(open) {
        details.open = open;
        animation = null;
        isClosing = false;
        isExpanding = false;
        details.style.height = details.style.overflow = "";
    }
}

function portalInitSmoothDetails(root) {
    (root || document).querySelectorAll("details.smooth-details").forEach(portalSmoothDetails);
}
document.addEventListener("DOMContentLoaded", function() { portalInitSmoothDetails(); });
document.addEventListener("htmx:afterSwap", function(evt) {
    var target = evt.detail && evt.detail.target;
    if (target) portalInitSmoothDetails(target);
});
