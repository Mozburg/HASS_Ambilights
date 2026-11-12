// Runs the calibration step planner straight out of the embedded UI, so the
// 4 / 10 / 12 contract is checked as behaviour, not as a string match.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const start = src.indexOf("function calSteps(mode) {");
if (start < 0) { console.error("calSteps not found"); process.exit(1); }
let depth = 0, end = start;
for (let i = src.indexOf("{", start); i < src.length; i++) {
  if (src[i] === "{") depth++;
  else if (src[i] === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
}
eval(src.slice(start, end));
const counts = { quick: calSteps("quick").length, refine: calSteps("refine").length,
                 full: calSteps("full").length };
const names = calSteps("full").map((s) => s.frame + ":" + s.level);
const ok = counts.quick === 4 && counts.refine === 10 && counts.full === 12
  && names[0] === "white:dark" && names[11] === "blue:light"
  && !calSteps("quick").some((s) => s.level !== "mid")
  && calSteps("refine").filter((s) => s.frame === "white").length === 1;
console.log(JSON.stringify({ counts, first: names[0], last: names[11], ok }));
process.exit(ok ? 0 : 1);
