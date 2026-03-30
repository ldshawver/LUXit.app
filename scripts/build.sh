#!/bin/bash
set -e

mkdir -p /tmp/bin_override
install -m 755 /dev/stdin /tmp/bin_override/uname << 'WRAPPER'
#!/bin/bash
case "$*" in
    *-r*-s*|*-s*-r*|*-rs*|*-sr*)
        rel=$(cat /proc/version 2>/dev/null | awk '{print $3}' || echo '5.15.0')
        echo "Linux $rel"
        ;;
    *-r*)
        cat /proc/version 2>/dev/null | awk '{print $3}' || echo '5.15.0'
        ;;
    *-s*)
        echo "Linux"
        ;;
    *-m*)
        echo "x86_64"
        ;;
    *-n*)
        hostname 2>/dev/null || echo "replit"
        ;;
    *-a*)
        rel=$(cat /proc/version 2>/dev/null | awk '{print $3}' || echo '5.15.0')
        echo "Linux $(hostname 2>/dev/null || echo replit) $rel x86_64"
        ;;
    *)
        echo "Linux"
        ;;
esac
WRAPPER

export PATH="/tmp/bin_override:$PATH"

echo "Testing uname wrapper..."
uname -rs && echo "uname -rs works!"

pip install --no-cache-dir -r requirements.txt

echo "Build completed successfully"
