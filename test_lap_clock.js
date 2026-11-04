"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
let now = 0;
const elements = [{textContent: ""}, {textContent: ""}];
const frames = [];
const window = {requestAnimationFrame: callback => frames.push(callback), dash_clientside: {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "assets/lap_clock.js"), "utf8"), {
    window, performance: {now: () => now}, document: {querySelectorAll: () => elements}
});
const clock = window.dash_clientside.telemetryClock;
assert.equal(clock.format(null), "—");
assert.equal(clock.format(59.9996), "1:00.000");
const anchor = {elapsed: 12.3, running: true, age: .02, max_extrapolation: .15, lap_key: "one"};
assert.ok(Math.abs(clock.estimate(anchor, .03) - 12.35) < 1e-9);
assert.ok(Math.abs(clock.estimate(anchor, 10) - 12.45) < 1e-9);
assert.equal(clock.estimate({...anchor, running: false}, 10), 12.3);
clock.update(anchor);
frames.shift()(30);
assert.equal(elements[0].textContent, "0:12.350");
assert.equal(elements[1].textContent, "0:12.350");
now = 40;
clock.update({...anchor, elapsed: 12.31, age: 0});
frames.shift()(40);
assert.equal(elements[0].textContent, "0:12.350"); // scheduling correction cannot go backwards
clock.update({...anchor, elapsed: .001, age: 0, lap_key: "two"});
frames.shift()(40);
assert.equal(elements[0].textContent, "0:00.001"); // next lap resets immediately
clock.update({...anchor, elapsed: .002, running: false, lap_key: "two"});
frames.shift()(4000);
assert.equal(elements[0].textContent, "0:00.002");
assert.equal(frames.length, 1); // only one animation loop, despite repeated updates
console.log("PASS live-clock interpolation, cap, pause, rounding, monotonicity and new-lap reset");
