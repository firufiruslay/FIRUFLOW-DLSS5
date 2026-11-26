#!/bin/sh
# Bakes the dusk home-page wordmark for this fork, the way tools/qt_probe/make-wordmark-dusk.py
# bakes the upstream one. That script starts from the author's own art, where the letters are
# filled with a palm-tree sunset; this fork has no such art, so the letters carry the dusk
# gradient the word "Veyra" itself wears, sampled from qml/Veyra/wordmark-dusk.png.
#
#   wordmark-firuflow.png        the lockup, cropped to the letters (1164x424)
#   wordmark-firuflow-glow.png   its soft glow, on the box grown by GLOW_PAD x the width on
#                                every side, at half size, for the intro bloom and the home
#                                page's breathing glow (the software scene graph has no
#                                MultiEffect, so the blur is baked here)
#   wordmark-firuflow-{dl,ss5,name}.png
#                                the three paper layers the startup intro flies apart and
#                                brings back together; stacked in one place they give the
#                                lockup back. DLSS5 splits at the gap between L and S, which
#                                seam.py found in the rendered alpha; FIRUFLOW is the layer
#                                that lands last and nearest, as "Veyra" does upstream.
#
# Needs ffmpeg only. Qt's software scene graph draws these as plain images, so everything
# here is baked, nothing is computed at runtime.
set -eu

FONT=${FONT:-/System/Library/Fonts/Supplemental/Arial Black.ttf}
OUT=${1:-qml/Veyra}
W=1164
H=424
ROW_CUT=160     # FIRUFLOW above, DLSS5 below
SEAM=467        # DL left, SS5 right
PAD=186         # GLOW_PAD 0.16 x the width, as upstream
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# The letters. x and y place the ink box itself: ffmpeg's drawtext puts an all-caps glyph box
# 2 px below y, and the left side bearing is 15 px at 206 and 24 px at 339.
ffmpeg -v error -y -f lavfi -i "color=c=black@0.0:s=${W}x${H},format=rgba" -frames:v 1 \
  -vf "drawtext=fontfile='$FONT':text='FIRUFLOW':fontcolor=white:fontsize=206:x=-15:y=-2,\
drawtext=fontfile='$FONT':text='DLSS5':fontcolor=white:fontsize=339:x=-24:y=173" "$WORK/mask.png"

# Dusk gradient, top to bottom, from wordmark-dusk.png: the purple of the V, the magenta of
# its middle, the pink at the top of the N, the salmon the palm scene ends on.
# Three linear segments, so the ramp holds each sampled colour where the art holds it.
ramp() { echo "if(lt(Y,141),$1+($2-$1)*Y/141,if(lt(Y,283),$2+($3-$2)*(Y-141)/141,$3+($4-$3)*(Y-283)/141))"; }
ffmpeg -v error -y -f lavfi -i "color=c=black:s=${W}x${H}" -frames:v 1 \
  -vf "format=gbrp,geq=r='$(ramp 94 161 236 239)':g='$(ramp 49 71 113 144)':b='$(ramp 132 148 170 121)'" \
  "$WORK/ramp.png"

# The letters wear the ramp; a soft dark offset behind them gives the paper depth the
# author's layers get from overlapping each other, which a two-row lockup never does.
ffmpeg -v error -y -i "$WORK/mask.png" -i "$WORK/ramp.png" \
  -f lavfi -i "color=c=0x1A0F2B:s=${W}x${H}" \
  -filter_complex "[0:v]alphaextract,split=2[ink][shade]; \
   [1:v][ink]alphamerge[letters]; \
   [shade]gblur=sigma=5[soft]; [2:v][soft]alphamerge,colorchannelmixer=aa=0.38[drop]; \
   color=c=black@0.0:s=${W}x${H},format=rgba[base]; \
   [base][drop]overlay=5:8[s1]; [s1][letters]overlay,format=rgba" \
  -frames:v 1 "$OUT/wordmark-firuflow.png"

# The glow: the same colours lifted towards white, blurred at 0.03 x the width.
ffmpeg -v error -y -i "$OUT/wordmark-firuflow.png" \
  -vf "pad=$((W + 2 * PAD)):$((H + 2 * PAD)):${PAD}:${PAD}:black@0.0,\
gblur=sigma=35:steps=4,eq=brightness=0.18:saturation=0.85,scale=$(((W + 2 * PAD) / 2)):-1:flags=lanczos" \
  -frames:v 1 "$OUT/wordmark-firuflow-glow.png"

# The paper layers: the lockup cut by rectangle, so stacking the three returns it exactly.
cut() {
  ffmpeg -v error -y -i "$OUT/wordmark-firuflow.png" \
    -f lavfi -i "color=c=black@0.0:s=${W}x${H},format=rgba" \
    -filter_complex "[0:v]crop=$1:$2:$3:$4[piece]; [1:v][piece]overlay=$3:$4,format=rgba" \
    -frames:v 1 "$OUT/wordmark-firuflow-$5.png"
}
cut "$W" "$ROW_CUT" 0 0 name
cut "$SEAM" "$((H - ROW_CUT))" 0 "$ROW_CUT" dl
cut "$((W - SEAM))" "$((H - ROW_CUT))" "$SEAM" "$ROW_CUT" ss5

for f in wordmark-firuflow wordmark-firuflow-glow wordmark-firuflow-dl wordmark-firuflow-ss5 wordmark-firuflow-name; do
  printf '%-34s %s\n' "$f.png" "$(ffprobe -v error -show_entries stream=width,height -of csv=p=0 "$OUT/$f.png")"
done
