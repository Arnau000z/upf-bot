# WhatsApp Cloud API — Configuració

## Credencials Meta

| Dada | Valor |
|---|---|
| **Phone Number ID** | `996354416903969` |
| **WABA ID** | `2389146764920165` |
| **Número de prova** | `+1 555 645 4095` |
| **Recipient de prova** | `+34 693 374 727` (número personal) |

> **Access Token** (temporal, caduca):
> Obtenir-lo a https://developers.facebook.com → l'app → WhatsApp → Configuració de l'API
> El token temporal dura ~24h. Per a producció cal generar un token permanent des de
> Meta Business Suite → Comptes del sistema.

## Webhook

| Dada | Valor |
|---|---|
| **URL callback** | `https://upfbot.claritmarket.org/webhook` |
| **Verify token** | `upfbot2026` |
| **Port local** | `8000` |
| **Script** | `whatsapp_webhook.py` |

## Cloudflare Tunnel

| Dada | Valor |
|---|---|
| **Hostname públic** | `upfbot.claritmarket.org` |
| **Destí intern** | `http://localhost:8000` |
| **Servei systemd** | `cloudflared` |
| **Domini** | `claritmarket.org` (comprat a Cloudflare Registrar) |

```bash
systemctl status cloudflared    # comprovar túnel
systemctl restart cloudflared   # reiniciar túnel
```

## Arrancar el servidor webhook

```bash
cd /root/upf-bot
nohup python3 whatsapp_webhook.py > whatsapp.log 2>&1 &
echo $! > whatsapp.pid
```

Aturar:
```bash
kill $(cat /root/upf-bot/whatsapp.pid)
```

## App Meta

- **App ID**: a la URL de developers.facebook.com quan estàs dins l'app
- **Estat**: en mode de desenvolupament (només pot rebre webhooks de prova)
- Per publicar-la cal verificació de negoci a Meta Business Suite

## Estat (2026-04-15)

- [x] Cloudflare Tunnel actiu
- [x] Webhook verificat per Meta
- [ ] Processar missatges entrants (lògica pendent a `whatsapp_webhook.py`)
- [ ] Número de telèfon de producció (ara mateix només el número de prova de Meta)
