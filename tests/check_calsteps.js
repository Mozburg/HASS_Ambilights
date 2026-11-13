// Runs the panel's own step list out of the embedded UI, so the order is checked as data.
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const match = src.match(/const CAL_STEPS = \[([^\]]+)\]/);
if (!match) { console.error("CAL_STEPS not found"); process.exit(1); }
const steps = match[1].split(",").map((s) => s.trim().replace(/^["']|["']$/g, ""));
const want = ["black", "grey", "white", "red", "green", "blue"];
const ok = steps.length === want.length && want.every((w, i) => steps[i] === w)
  && src.includes("const CAL_FRAMES = { black: [0, 0, 0]")
  && src.includes("calCaps")          // the caps are computed from the chosen brightnesses
  && !src.includes("calSteps(");      // the old 4/10/12 planner is gone
if (!ok) { console.error(JSON.stringify(steps)); process.exit(1); }
console.log(`steps ${steps.join(" -> ")}`);
