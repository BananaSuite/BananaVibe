# Example: BananaWiki + BananaChat on a VPS

A real-world run: harden, refactor and optimise two apps, then refresh their landing pages and screenshots.

```bash
# On the VPS, as the user that will run it (root works too)
mkdir -p ~/bananasuite && cd ~/bananasuite
git clone https://github.com/BananaSuite/BananaWiki
git clone https://github.com/BananaSuite/BananaChat
git clone <landing site repositories, if separate>

bananavibe init --name bananasuite --workers claude,codex --reviewer codex
cp /path/to/BananaVibe/examples/bananasuite/GOAL.md .bananavibe/GOAL.md
cp /path/to/BananaVibe/examples/bananasuite/config.toml .bananavibe/config.toml
$EDITOR .bananavibe/GOAL.md .bananavibe/config.toml   # landing page paths, real test commands
bananavibe doctor --live
bananavibe start
```

Review cadence: with `checkpoint_every = 5` you get a report after every 5 sessions in
`.bananavibe/reports/latest.md` (plus a cross-model review whose findings go back to the workers). To stop and read
each one before it continues, set `pause_at_checkpoint = true` and continue with `bananavibe resume`.

When it finishes (`bananavibe status` says `done`), review the `bananavibe/work` branch in each repository:

```bash
cd ~/bananasuite/BananaWiki && git log --oneline main..bananavibe/work && git diff --stat main...bananavibe/work
```
