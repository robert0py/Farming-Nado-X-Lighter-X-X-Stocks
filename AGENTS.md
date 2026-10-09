# Project instructions

- Current task: delegated trading authorization and hedge execution, preserving the working balance viewer.
- Product direction: a working application built incrementally, not an MVP. Use the user's dark dashboard reference, with Balance and Trading navigation. Never ship sample financial values, simulated trades/history, or runtime demo modes. Synthetic fixtures belong only in isolated tests.
- The user authorized the trading stage. Use separate delegated/API signing keys; never request the main wallet private key. No deposits, transfers, or withdrawal methods are in scope.
- Persist order intent before submission, reconcile uncertain executions without blind write retries, and prevent new cycles while exposure is unresolved. No automatic trading until explicit strategy start in the app.
- No server exists yet. Deliver an independently running local worker and Linux deployment package; do not claim VPS deployment before it exists.
- Research API changes using official Nado and Lighter Robinhood Chain documentation. Do not substitute Lighter Core.
- Use `find-skills` from vercel-labs/skills and skills.sh to find appropriate skills as work evolves.
- After every coherent code change, immediately commit and push it to this private repository. Never commit credentials, wallet private keys, account configuration, real balance snapshots, or logs.
- Desktop delivery uses the Windows GitHub Actions workflow: every completed code push to main must pass tests and publish a private desktop release. Check the workflow result and repair failures.
- After server deployment has been established, deploy every subsequent completed server code update to that configured destination. No server deployment destination is configured yet.
- Preserve the future requirements in docs/ROADMAP.md. They are context, not authorization to expand the current task.
