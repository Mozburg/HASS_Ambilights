// Checks the panel's two calibration flows and their order, straight out of the UI source.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

const flows = {};
const block = src.match(/const CAL_FLOWS = \{([\s\S]*?)\};/);
if (!block) { console.error("CAL_FLOWS not found"); process.exit(1); }
for (const m of block[1].matchAll(/(\w+):\s*\[([^\]]*)\]/g)) {
  flows[m[1]] = m[2].split(",").map((s) => s.trim().replace(/^["']|["']$/g, ""));
}

const want = { level: ["black", "grey", "white"], colour: ["red", "green", "blue"] };
let ok = Object.keys(want).every((k) =>
  flows[k] && flows[k].length === want[k].length && want[k].every((s, i) => flows[k][i] === s));
ok = ok
  && src.includes("calibrate-color")            // both buttons exist
  && !src.includes("calSteps(")                 // the old 4/10/12 planner is gone
  && src.includes("if (CAL.flow === \"level\")")  // a flow commits only its own points

if (!ok) { console.error(JSON.stringify(flows)); process.exit(1); }
console.log(`flows ${Object.entries(flows).map(([k, v]) => `${k}: ${v.join(" -> ")}`).join(" | ")}`);
