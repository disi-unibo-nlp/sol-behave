"""
patch_deploy_server.py
======================
Adds read_view message handling to the _SERVER_JS string in deploy_replay.py.
This lets the Python side call zero-arg view functions on the deployed contract
and get their return values as a JSON dict.
"""
import sys
from pathlib import Path

target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../utils/deploy_replay.py")
src = target.read_text()
n = 0

# ── PATCH 1: add read_view handler to _SERVER_JS ─────────────────────────────
# Insert before the closing `process.exit(0)` in the main() function of _SERVER_JS

OLD_SERVER_EXIT = '''    process.exit(0);
}
main();'''

NEW_SERVER_EXIT = '''    process.exit(0);
}

async function readViewState(contractAddress, selectors) {
  const result = {};
  if (!contractAddress || !selectors || selectors.length === 0) return result;
  const provider = ethers.provider;
  for (const sel of selectors) {
    try {
      const data = "0x" + sel.selector;
      const raw  = await provider.call({ to: contractAddress, data });
      // Decode based on output types
      if (!sel.outputTypes || sel.outputTypes.length === 0) {
        result[sel.name] = raw;
      } else if (sel.outputTypes.length === 1) {
        const decoded = ethers.AbiCoder.defaultAbiCoder().decode(
          sel.outputTypes, raw
        );
        // Convert BigInt to string for JSON serialisation
        result[sel.name] = decoded[0] === undefined ? null
          : typeof decoded[0] === "bigint" ? decoded[0].toString()
          : decoded[0];
      } else {
        const decoded = ethers.AbiCoder.defaultAbiCoder().decode(
          sel.outputTypes, raw
        );
        result[sel.name] = decoded.map(v =>
          typeof v === "bigint" ? v.toString() : v
        );
      }
    } catch (_) {
      result[sel.name] = null;
    }
  }
  return result;
}

main();'''

# Also need to handle the read_view message in the readline loop
OLD_TX_HANDLER = '''    if (msg.tx) {
      if (!contractAddress) {
        process.stdout.write(JSON.stringify({
          hash: msg.tx.hash, events_match: false,
          actual_status: "error", error: "not deployed"
        }) + "\\n");
        continue;
      }
      const result = await replayTx(msg.tx, contractAddress);
      process.stdout.write(JSON.stringify(result) + "\\n");
      continue;
    }
  }'''

NEW_TX_HANDLER = '''    if (msg.tx) {
      if (!contractAddress) {
        process.stdout.write(JSON.stringify({
          hash: msg.tx.hash, events_match: false,
          actual_status: "error", error: "not deployed"
        }) + "\\n");
        continue;
      }
      const result = await replayTx(msg.tx, contractAddress);
      process.stdout.write(JSON.stringify(result) + "\\n");
      continue;
    }

    if (msg.read_view) {
      const selectors  = msg.read_view.selectors || [];
      const view_state = await readViewState(contractAddress, selectors);
      process.stdout.write(JSON.stringify({ view_state }) + "\\n");
      continue;
    }
  }'''

if OLD_SERVER_EXIT in src and "readViewState" not in src:
    src = src.replace(OLD_SERVER_EXIT, NEW_SERVER_EXIT, 1)
    n += 1
    print("  ✓ patch 1: added readViewState JS function")
else:
    print("  ✗ patch 1 skipped (already applied or anchor not found)")

if OLD_TX_HANDLER in src and "msg.read_view" not in src:
    src = src.replace(OLD_TX_HANDLER, NEW_TX_HANDLER, 1)
    n += 1
    print("  ✓ patch 2: added read_view message handler in JS loop")
else:
    print("  ✗ patch 2 skipped (already applied or anchor not found)")

target.write_text(src)
print(f"\nApplied {n} patches to {target}")

import ast
try:
    ast.parse(src)
    print("Python syntax OK")
except SyntaxError as e:
    print(f"SYNTAX ERROR: {e}")