## drain3-producer (test)

Petit service de test qui:

- lit des logs depuis Loki (API `query_range`)
- applique Drain3 pour générer des templates (clusters)
- envoie un webhook à `rca-service` sur `POST /webhook/drain3` quand un cluster dépasse un seuil

### Variables d'environnement

- `LOKI_URL` (default: `http://loki:3100`)
- `LOKI_QUERY` (default: `{job=~".+"}`)  
  Loki exige un sélecteur de labels (ex: `{service_name="school-service"}`).
- `LOOKBACK_SECONDS` (default: `45`)
- `POLL_INTERVAL_SECONDS` (default: `10`)
- `WINDOW_SECONDS` (default: `120`)
- `NOTIFY_THRESHOLD` (default: `5`)
- `NOTIFY_COOLDOWN_SECONDS` (default: `180`)
- `RCA_WEBHOOK_URL` (default: `http://rca-service:8000/webhook/drain3`)
- `MAX_LINES_PER_POLL` (default: `300`)

