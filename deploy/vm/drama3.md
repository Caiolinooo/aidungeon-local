# Drama-3 nesta VM

Drama-3 é o TTS da Fish Audio lançado em 13 de outubro de 2026
([modelo](https://huggingface.co/fishaudio/drama-3),
[como dirigir a fala](https://docs.fish.audio/features/text-to-speech/drama-3)).
Pesos em `fishaudio/drama-3`: cerca de 35B de parâmetros, uns 3B ativos por
token, experts em FP8 e o resto em bf16. A licença é a Fish Audio Community
License: uso de pesquisa e não comercial, e uso comercial enquanto a receita
anual fica abaixo de US$ 500 mil. Oferecer o modelo como serviço hospedado
para terceiros pede licença à parte. O header da API deles é `model: drama-3`.
A emoção vai em colchetes antes da frase, em linguagem comum
(`[frightened, breath short]`), ou numa tag pareada (`<whisper>…</whisper>`).

## Por que não roda aqui

Medido na VM (L4, sem parar o narrador nem o gerador de imagem):

| | |
|---|---|
| GPU | NVIDIA L4, 23034 MiB |
| Em uso | 17144 MiB |
| Livre | 5422 MiB |
| llama-server (Qwen) | 14220 MiB |
| embeddings | 698 MiB |
| RAM | 49 GiB no total, cerca de 10 GiB disponíveis |

O cartão da Hugging Face publica o serviço em GPU H200. Um relato de
implantação pede em geral 48 GB ou mais de VRAM
([AICoder, 14 out 2026](https://aicoder.com/news/news-20261014-fish-audio-drama-3-open-weight-tts-moe)).
5 GB livres não seguram um checkpoint de 35B. A RAM que sobra também não
segura os experts fora da GPU. O `abz-voice-audio.service`
(`/home/caio/Desktop/abz-voice-local/audio_server.py`) só carrega Whisper e
Supertonic 3. O campo `model` do pedido é ignorado; `instructions` existe no
schema e hoje não é usado.

`voice.drama3_enabled` fica `false` em `config/settings.json`. Não ligar
neste hardware: o servidor atual devolveria 200 e o Supertonic leria o
colchete em voz alta.

## O que o app faz hoje

`POST /api/tts` aceita `emotion` e `adventure_id`. A emoção sai da linha
(joy, anger, fear, sadness, tenderness, surprise, disgust, neutral). Se a
linha não disser, entra a ficha do NPC citado. A resposta traz
`X-Voice-Emotion`. O cliente de voz manda a direção em `instructions` e
fala com `supertonic-3`, texto limpo.

Quando um dia o servidor de voz carregar o Drama-3 de verdade, ponha
`drama3_enabled` em `true`. Aí o pedido usa `drama3_model` (`drama-3`) e
prefixa a direção. Se esse modelo recusar, a fala volta ao Supertonic sem
a etiqueta.
