# 📊 Stack d'Observabilité Centralisé - Digital Factory

Ce projet implémente une architecture de monitoring moderne (Logs & Traces) pour la Digital Factory. Il permet de déployer de manière **100% automatisée** un stack complet : **Loki**, **Jaeger**, **Grafana** et **OpenTelemetry**.

---

## 🏗️ 1. Architecture et Flux de Données

Le système fonctionne selon un flux de données précis pour garantir une visibilité totale :

1.  **Collecte (OpenTelemetry Collector)** : Reçoit les logs et traces des applications.
2.  **Stockage des Logs (Loki)** : Reçoit et indexe les logs envoyés par l'Otel Collector.
3.  **Stockage des Traces (Jaeger)** : Reçoit et stocke les traces distribuées pour l'analyse de performance.
4.  **Visualisation (Grafana)** : Centralise les données et permet la corrélation entre un log et sa trace associée.

---

**Grafana** : http://localhost:3000, Visualisation et Alertes (admin/admin)
**Jaeger UI** : http://localhost:16686 ,Analyse des Traces (Waterfall charts)
**Loki API** : http://localhost:3100, Moteur de recherche de logs

---

## 🚀 2. Déploiement Automatisé (Ansible)

Pour installer le projet sur un nouveau PC, une seule commande suffit. Le Playbook Ansible s'occupe de tout :
- **Installation** : Mise à jour du système et installation de Docker/Docker-Compose.
- **Configuration** : Création des répertoires et copie de tous les fichiers de configuration.
- **Orchestration** : Lancement du stack via Docker Compose.

### Commande de lancement :
```bash
ansible-playbook -i inventory.ini playbook.yml