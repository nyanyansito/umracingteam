(function () {
    "use strict";
    let anchor = null;
    let receivedAt = 0;
    let lastValue = null;
    let lapKey = null;
    let started = false;

    function estimate(data, sinceReceive) {
        if (!data || data.elapsed === null) return null;
        if (!data.running) return data.elapsed;
        return data.elapsed + Math.min(data.max_extrapolation, Math.max(0, data.age + sinceReceive));
    }

    function format(seconds) {
        if (seconds === null) return "—";
        const milliseconds = Math.round(Math.max(0, seconds) * 1000);
        const minutes = Math.floor(milliseconds / 60000);
        const remainder = milliseconds % 60000;
        return minutes + ":" + String(Math.floor(remainder / 1000)).padStart(2, "0")
            + "." + String(remainder % 1000).padStart(3, "0");
    }

    function frame(now) {
        if (anchor) {
            let value = estimate(anchor, (now - receivedAt) / 1000);
            // Small scheduling corrections must not make a running timer
            // count backwards. A new lap or paused clock accepts its anchor.
            if (anchor.running && value !== null && lastValue !== null) value = Math.max(value, lastValue);
            lastValue = value;
            const text = format(value);
            document.querySelectorAll("[data-lap-clock]").forEach(function (element) {
                if (element.textContent !== text) element.textContent = text;
            });
        }
        window.requestAnimationFrame(frame);
    }

    window.dash_clientside = Object.assign({}, window.dash_clientside, {
        telemetryClock: {
            estimate: estimate,
            format: format,
            update: function (data) {
                if (!data) return window.dash_clientside.no_update;
                if (lapKey !== data.lap_key) lastValue = null;
                lapKey = data.lap_key;
                anchor = data;
                receivedAt = performance.now();
                if (!started) {
                    started = true;
                    window.requestAnimationFrame(frame);
                }
                return data.lap_key;
            }
        }
    });
})();
