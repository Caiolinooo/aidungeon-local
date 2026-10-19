# Fatos do mundo

O pedido do jogador é tentativa. Lugar, item, pessoa e consentimento só entram se a abertura, a narração, uma ação Story, o Plot, um card ou uma memória já os estabeleceu.

1. Julgar destino, item e ordem contra o cânone (`backend/app/world_facts.py`).
2. Injetar o bloco `[Facts]` no narrador e nas sugestões.
3. Descartar narração que cumpre um pedido bloqueado (quarto inventado, NPC que obedece sem motivo).
4. Pintar o See e o fundo com o lugar real e a recusa, não com o desejo.
5. Mostrar o aviso na faixa da cena (`notice`).
6. Impedir o registro de lugar/item/obediência que só existiu no pedido.
7. Travar o sexo na imagem: mulher fica mulher, homem fica homem, a frase vai no começo do prompt.
