#!/bin/bash
# Скачивает модели в models/ (или в папку из первого аргумента) с HuggingFace по закреплённым версиям
# и сверяет sha256 — подменённый или битый файл не пройдёт.
#   GigaAM v3 e2e (Сбер, MIT), ONNX int8 от автора onnx-asr — русская речь с пунктуацией, 216 МБ
#   Whisper small (OpenAI, MIT), сборка CTranslate2 от SYSTRAN — запасной для других языков, 467 МБ
set -euo pipefail
DIR=${1:-"$(cd "$(dirname "$0")/.." && pwd)/models"}
HF=https://huggingface.co
GIGAAM=istupakov/gigaam-v3-onnx@322c3b29492673eb7d0b434bfa9dfb8653e34d02
WHISPER=Systran/faster-whisper-small@536b0662742c02347bc0e980a01041f333bce120

get() {     # папка репо@версия файл sha256
  local dst="$DIR/$1/$3" repo=${2%@*} rev=${2#*@}
  mkdir -p "$DIR/$1"
  if [[ -f "$dst" ]] && echo "$4  $dst" | shasum -a 256 -c --status 2>/dev/null; then return; fi
  echo "качаю $1/$3"
  curl -fL --retry 3 -o "$dst.part" "$HF/$repo/resolve/$rev/$3"
  echo "$4  $dst.part" | shasum -a 256 -c --status || { rm -f "$dst.part"; echo "не сошлась сумма: $3"; exit 1; }
  mv "$dst.part" "$dst"
}

get gigaam $GIGAAM config.json 0641fcf73f4af791c73f05083e38a658ff5dcbee3534a0c61396c10f4b97f0fe
get gigaam $GIGAAM v3_e2e_rnnt_encoder.int8.onnx 4e0e076a6076cd110277e529b8ac8f32cd5297f7fbebad5341ae8ddb7d00817b
get gigaam $GIGAAM v3_e2e_rnnt_decoder.int8.onnx 89014e134865615b91e037157e46e389b1271e6072460efc010ea08e61e23146
get gigaam $GIGAAM v3_e2e_rnnt_joint.int8.onnx ade116563dbf66e503b0994efab6b5861412743e52bf31c39fc3fffa3783d5d1
get gigaam $GIGAAM v3_e2e_rnnt_vocab.txt 39abae20e692998290c574e606f11a9edef2902a1995463fcff63d1490cf22b7
get whisper-small $WHISPER config.json b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828
get whisper-small $WHISPER model.bin 3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671
get whisper-small $WHISPER tokenizer.json fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab
get whisper-small $WHISPER vocabulary.txt 34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913
echo "модели на месте: $DIR"
