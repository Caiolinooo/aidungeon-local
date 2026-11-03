## Context

O guia `docs/engines/README.md` do VoiceStudio lista o motor que o jogo usa: OmniVoice GGUF, o mesmo modelo do OmniVoice padrão, em binário nativo, com clonagem e saída 24 kHz mono. A conferência contra o binário da VM (`omnivoice-tts` `886fc07`) e contra `backend/engines/omnivoice_gguf/backend.py` mostra que o servidor de voz e o app já estão nesse contrato. Não há diff local: `main` é `56ea7bc`, igual a `origin/main`, e a VM já publicou esse commit em main e staging. O fim deste plano é reiniciar só o processo de voz e o app principal para o teste, sem outro deploy e sem mudar código.

## Approach

1. Não editar o repositório, não commitar e não rodar `tools/deploy.sh`. O app em `/home/caio/aidungeon` e o staging já estão em `56ea7bc40c01ae3abae4c70511d121b301739b60`. Um segundo deploy só reconstruiria o mesmo frontend e derrubaria o jogo outra vez.

2. Manter o contrato que já confere com o guia. Não alterar `audio_server.py` nem o app:
   - Quantização `omnivoice-base-Q8_0.gguf` e tokenizer `omnivoice-tokenizer-Q8_0.gguf`. A tabela do guia dá Q8_0 para 4–12 GB (~945 MB) e BF16 para 12 GB+ (~1,6 GB). A L4 está compartilhada e sobrou cerca de 3 GB livres na prova; Q8_0 é a linha que cabe. Não trocar para BF16.
   - Cada fala nasce um processo novo: `omnivoice-tts --model … --codec … --lang Portuguese --ref-rvq <voz.rvq> --ref-text <ref.txt> --seed 42 -o <wav>`, texto no stdin, sob `_omni_lock`, timeout 90 s. `--lang Portuguese` é o rótulo que o adaptador do VoiceStudio usa para `pt`. `--ref-text` aponta para arquivo, como o adaptador faz. `--ref-wav` continua de fora: nesta GPU ele dispara `SOFT_MAX`. `--ref-rvq` é a flag deste binário para o código já gerado pelo `omnivoice-codec`.
   - Não passar `--instruct` e não colocar a emoção no texto falado. O GGUF aceita `--instruct` e declara `supports_voice_design = False`. O modelo não atua etiquetas `[angry]`/`[sad]`; o conjunto não verbal dele é outro (`[laughter]`, `[sigh]` e as interjeições listadas em `docs/expressive-speech.md`). `drama3_enabled` permanece false. O texto da história segue literal, então uma tag não verbal que já esteja na frase chega ao modelo.
   - Clones `cv_` + 8 hex continuam com transcrição obrigatória (o CLI exige `--ref-text`) e duração 1–30 s. O app do VoiceStudio recusa clipe com transcrição acima de 20 s e recomenda 3–10 s; o nosso codec já codifica o clipe inteiro e um clone de prova passou. Não apertar o limite neste reinício.
   - `model` diferente de `omnivoice` continua no Supertonic, inclusive `tts-1` do LiveKit.

3. Na VM, por SSH `ssh -o BatchMode=yes caio@vm.groupabz.com`, reiniciar só dois units. `sudo -n` falha com “uma senha é necessária”. Criar o askpass no remoto, usar e apagar no `trap` mesmo se o restart falhar:

```bash
install -m 700 /dev/null /tmp/askpass-aid
printf '%s\n' '#!/bin/sh' 'printf "%s\n" "$VM_SUDO_PASSWORD"' > /tmp/askpass-aid
chmod 700 /tmp/askpass-aid
export SUDO_ASKPASS=/tmp/askpass-aid
export DISPLAY=:0
trap 'rm -f /tmp/askpass-aid' EXIT
sudo -A systemctl restart abz-voice-audio.service
sudo -A systemctl restart aidungeon
```

Não reiniciar `aidungeon-staging`. Não imprimir a senha, `AUTH_SECRET` nem `AUDIO_API_KEY`.

4. Esperar no máximo 60 s. Sucesso do restart: `systemctl is-active abz-voice-audio.service` e `systemctl is-active aidungeon` imprimem `active`, e `curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:2122/api/adventures` imprime `401`. Se aos 60 s a porta seguir em `000` ou o unit não estiver `active`, rodar uma vez `journalctl -u aidungeon -n 40 --no-pager` e parar. Não disparar outro restart.

5. Se `GET http://127.0.0.1:8001/health` não contiver `"omnivoice": "q8-cli"`, parar e reportar. Não reescrever `audio_server.py`.

6. Conferir a config efetiva sem imprimir segredo. No checkout `/home/caio/aidungeon-git`, usar `tools/replay_turn.py` (`read_dotenv` + `mint_token`) com `/home/caio/aidungeon/.env`. `GET http://127.0.0.1:2122/api/settings/voice` deve ter `tts_model` igual a `omnivoice`. Se o override ainda for outro valor, `PUT` somente `{"tts_model":"omnivoice"}` e ler de novo. Não alterar `narrator_voice` (hoje `F2`). `GET /api/voices` deve trazer `voices` `M1`…`F3` e a chave `cloned`.

## Critical files & anchors

- `/home/caio/Desktop/abz-voice-local/audio_server.py` — `_synthesize_omnivoice` (~linha 154) e o ramo `model == "omnivoice"` em `POST /v1/audio/speech`. Fora do git do jogo; não editar.
- `/home/caio/voicestudio-build/omnivoice.cpp/build/omnivoice-tts` — `--help` confirma `--lang`, `--instruct`, `--ref-rvq`, `--ref-text`, `--seed`. SHA `886fc07`.
- `backend/app/voice_client.py` — `synthesize` manda `instructions` à parte e só troca o modelo para Drama-3 se `drama3_enabled`.
- `config/settings.json` — `voice.tts_model` já é `omnivoice`; o banco da VM pode sobrepor. O passo 6 cobre o override.
- `tools/deploy.sh` — não executar. O restart daqui é `systemctl`, não esse script.

## Verification

Na VM, depois do restart e com o token do passo 6:

- `curl -sS http://127.0.0.1:8001/health` contém `"omnivoice": "q8-cli"` e HTTP 200.
- `GET /api/settings/voice` devolve `tts_model` `omnivoice` e `enabled` true.
- `POST http://127.0.0.1:2122/api/tts` com `Authorization: Bearer <token>`, `Content-Type: application/json` e corpo `{"text":"Olá, esta é a voz do narrador.","voice":"F2"}`. Esperado: HTTP 200, `Content-Type` contendo `audio/mpeg`, corpo com mais de 1000 bytes. Gravar em `/tmp/omni-test.mp3` e imprimir só status, content-type e tamanho. `curl --max-time 300`. Se voltar 500, copiar a última linha de `journalctl -u abz-voice-audio.service -n 20 --no-pager` e parar, sem mudar timeout nem flags.
- `test -f /tmp/askpass-aid` deve falhar depois do script.

## Assumptions & contingencies

- Exporte `VM_SUDO_PASSWORD` no shell remoto antes de criar o askpass; não grave a senha neste arquivo. Se `sudo -A` recusar, apagar o askpass e parar; não tentar outra senha e não deixar o arquivo.
- Se `/home/caio/aidungeon/DEPLOYED` não começar com `commit=56ea7bc40c01ae3abae4c70511d121b301739b60`, parar. Não fazer deploy neste plano.
- Se o `POST /api/tts` passar de 300 s, tratar como falha do teste e parar. Não subir `OMNI_TIMEOUT_S`.
