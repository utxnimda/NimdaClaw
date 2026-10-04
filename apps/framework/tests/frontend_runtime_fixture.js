"use strict";

const fs = require("node:fs");
const path = require("node:path");
const commonSource = fs.readFileSync(path.resolve(__dirname, "../frontend/src/common/runtime.js"), "utf8");

// Match the browser's dependency order without starting an application or API.
exports.withCommonRuntime = function (source) {
  return commonSource + '\nif (typeof localStorage !== "undefined") window.localStorage = localStorage;\n' + source;
};
