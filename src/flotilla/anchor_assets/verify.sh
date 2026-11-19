# BusyBox-only self-containment check: run in the built anchor with no network, mounts, or host
# environment. Every flotilla operation the anchor performs is one of these applets.
set -e
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
B=/.flotilla/bin/busybox

# Applets the anchor invokes by absolute path, plus those its PATH-resolved commands need.
list="$("$B" --list)"
for applet in sh sleep mkdir rm ls chown chmod cp mv true stat find awk env id base64; do
    case "
$list
" in
        *"
$applet
"*) ;;
        *) echo "anchor BusyBox lacks applet: $applet" >&2; exit 1 ;;
    esac
done

# PATH-resolved applets must actually resolve: the anchor's commands name them plainly.
mkdir /tmp/anchor-path-check && rm -rf /tmp/anchor-path-check
sleep 0

# Root is the identity the anchor's chown/chmod operations assume.
[ "$("$B" id -u)" = 0 ] || { echo "anchor must default to root" >&2; exit 1; }

# Clear a directory in place without replacing its inode: the operation that stages verifier
# output directories, exercised with nested directories, plain files, and a symlink.
work=/tmp/anchor-clear-check
"$B" mkdir -p "$work/nested"
echo 1 >"$work/nested/payload"
echo 1 >"$work/reward.txt"
"$B" ln -s reward.txt "$work/link"
inode="$("$B" stat -c %i "$work")"
"$B" find "$work" -mindepth 1 -delete
[ -z "$("$B" ls -A "$work")" ] || { echo "find did not clear the directory" >&2; exit 1; }
[ "$("$B" stat -c %i "$work")" = "$inode" ] || { echo "find replaced the directory inode" >&2; exit 1; }
"$B" rm -rf "$work"

# Archive round trip through the binary that stages and downloads workspaces.
tree=/tmp/anchor-tar-check
"$B" mkdir -p "$tree/src"
echo "anchor self-containment" >"$tree/src/payload"
"$B" tar -cf "$tree/payload.tar" -C "$tree/src" .
"$B" mkdir "$tree/dst"
"$B" tar -xf "$tree/payload.tar" -C "$tree/dst"
"$B" cmp "$tree/src/payload" "$tree/dst/payload" || { echo "anchor archive round trip failed" >&2; exit 1; }
"$B" rm -rf "$tree"

echo "anchor_check ok"
