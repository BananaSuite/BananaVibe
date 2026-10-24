#!/bin/sh
# Installs a `bananavibe` launcher in ~/.local/bin that runs this checkout (no pip needed).
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
bin="${BIN_DIR:-$HOME/.local/bin}"
python="$(command -v python3)"
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11+ is required" >&2; exit 1; }
mkdir -p "$bin"
cat > "$bin/bananavibe" <<LAUNCHER
#!/bin/sh
PYTHONPATH="$root\${PYTHONPATH:+:\$PYTHONPATH}" exec "$python" -P -m bananavibe "\$@"
LAUNCHER
chmod +x "$bin/bananavibe"
echo "Installed $bin/bananavibe"
case ":$PATH:" in *":$bin:"*) ;; *) echo "Add $bin to your PATH." ;; esac
