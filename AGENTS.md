# Project instructions

- Current release scope: desktop account connections and balance viewing only.
- Product direction: a working application built incrementally, not an MVP. Use the user's dark dashboard reference, with Balance and Trading navigation. Never ship sample financial values, simulated trades/history, or runtime demo modes. Synthetic fixtures belong only in isolated tests.
- Do not implement trading, signing, deposits, withdrawals, or secret-key collection without a new task.
- Research API changes using official Nado and Lighter Robinhood Chain documentation. Do not substitute Lighter Core.
- Use `find-skills` from vercel-labs/skills and skills.sh to find appropriate skills as work evolves.
- After every coherent code change, immediately commit and push it to this private repository. Never commit credentials, wallet private keys, account configuration, real balance snapshots, or logs.
- Desktop delivery uses the Windows GitHub Actions workflow: every completed code push to main must pass tests and publish a private desktop release. Check the workflow result and repair failures.
- After server deployment has been established, deploy every subsequent completed server code update to that configured destination. No server deployment destination is configured yet.
- Preserve the future requirements in docs/ROADMAP.md. They are context, not authorization to expand the current task.
