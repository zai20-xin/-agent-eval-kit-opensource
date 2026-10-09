#!/bin/bash
# T1 opencode 批次：N=6/7/8 各 K=20，验证遗漏率随 N 的趋势
# 每次 perl 150s 兜底（opencode 收尾必挂起）；read=0 视为失败重试一次；顺序跑避免 SQLite 并发锁
set -u
ROOT=~/tmp/opencode-trace-demo/t1
RESULTS=$ROOT/results.csv
PROMPT="请先读全当前目录下所有 .py 文件，然后回答：每个文件各定义了几个函数？"
echo "tier,run,sid,read_count,omitted" > "$RESULTS"

get_sid() {
  opencode db "SELECT id FROM session ORDER BY time_created DESC LIMIT 1" --format json \
    | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])" 2>/dev/null
}
get_reads() {
  local SID="$1" N="$2"
  opencode db "SELECT COUNT(DISTINCT json_extract(data,'\$.state.input.filePath')) AS n FROM part WHERE session_id='$SID' AND json_extract(data,'\$.type')='tool' AND json_extract(data,'\$.tool')='read'" --format json \
    | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['n'])" 2>/dev/null
}

for N in 6 7 8; do
  DIR=$ROOT/n$N
  mkdir -p "$DIR"
  for f in $(seq 1 $N); do echo "def f_$f(x): return x*$f" > "$DIR/f$f.py"; done
  for i in $(seq 1 20); do
    cd "$DIR"
    perl -e 'alarm(shift); exec @ARGV' 150 opencode run --auto "$PROMPT" >/dev/null 2>&1
    SID=$(get_sid)
    if [ -z "$SID" ]; then
      echo "$N,$i,EMPTY,0,1" >> "$RESULTS"
      echo "tier=$N run=$i EMPTY(invalid)"
      sleep 8; continue
    fi
    RC=$(get_reads "$SID" "$N")
    RC=${RC:-0}
    OM=0; [ "$RC" -lt "$N" ] && OM=1
    # read=0 说明可能挂起在读取前，重试一次
    if [ "$RC" -eq 0 ]; then
      sleep 15
      perl -e 'alarm(shift); exec @ARGV' 150 opencode run --auto "$PROMPT" >/dev/null 2>&1
      SID=$(get_sid)
      RC=$(get_reads "$SID" "$N"); RC=${RC:-0}
      [ "$RC" -lt "$N" ] && OM=1 || OM=0
    fi
    echo "$N,$i,$SID,$RC,$OM" >> "$RESULTS"
    echo "tier=$N run=$i SID=$SID read=$RC omitted=$OM"
    sleep 8
  done
done

# 汇总：各档遗漏率 + Wilson 95% CI
python3 - <<'PY'
import csv, math, os
ROOT="~/tmp/opencode-trace-demo/t1"
rows=list(csv.DictReader(open(os.path.join(ROOT,"results.csv"))))
def wilson(k,n,z=1.96):
    if n==0: return (0.0,0.0)
    p=k/n; den=1+z*z/n; c=(p+z*z/(2*n))/den
    h=(z/den)*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return (max(0,c-h),min(1,c+h))
from collections import defaultdict
bytier=defaultdict(list)
for r in rows:
    if r['sid']=='EMPTY': continue
    bytier[int(r['tier'])].append(int(r['omitted']))
out=[]
out.append("== T1 opencode 各档遗漏率（K=20，含 Wilson 95% CI）==")
for N in sorted(bytier):
    om=bytier[N]; k=sum(om); n=len(om)
    lo,hi=wilson(k,n)
    out.append(f"N={N}: 遗漏 {k}/{n} = {k/n*100:.1f}%   CI[{lo*100:.1f}%, {hi*100:.1f}%]")
out.append("== 备注：read=0 已重试一次；EMPTY 为 opencode 未建 session（计 invalid，已排除）==")
open(os.path.join(ROOT,"summary.txt"),"w").write("\n".join(out)+"\n")
print("\n".join(out))
PY
echo "BATCH DONE"
