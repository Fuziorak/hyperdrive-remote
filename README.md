# Hyperdrive Remote

Interface web event-driven pour piloter les véhicules Anki Overdrive via MQTT (réseau `hyperdrive` — UNIFR).

**Repo :** [github.com/Fuziorak/hyperdrive-remote](https://github.com/Fuziorak/hyperdrive-remote)

## Prérequis

1. Rejoindre le Wi‑Fi **SSID `hyperdrive`**
2. Broker MQTT : `10.42.0.1:1883` (user MQTT Explorer : `Your_mom`, souvent sans mot de passe)
3. Python 3.10+

## Installation

```bash
git clone https://github.com/Fuziorak/hyperdrive-remote.git
cd hyperdrive-remote
pip install -r requirements.txt
```

## Lancer

**Windows (recommandé) :** double-clic sur le raccourci bureau, ou :

```bash
wscript start_hyperdrive.vbs
```

**Manuel :**

```bash
python app.py
```

Puis ouvrir [http://127.0.0.1:5050](http://127.0.0.1:5050).

**Test MQTT seul (sur le Wi‑Fi hyperdrive) :**

```bash
python test_mqtt.py
```

## Utilisation

1. **Connecter MQTT**
2. **Scanner les véhicules** (discover) — ou saisir un ID manuellement
3. Assigne chaque véhicule à la **piste A** et/ou **piste B**
4. **Connecter** dans chaque cockpit, puis piloter (vitesse / voie / feux)

### Mode course
- **Départ** / **Tour** / **Fin chrono** / **Reset**
- **STOP ALL** coupe la vitesse des deux pistes

### Profils
- Intégrés : `Course`, `Feux rouge`, `Flag tail flash`
- Sauve tes propres configs (localStorage navigateur) puis applique sur A, B ou A+B

Preset **Queue flash ★** = feux arrière clignotants (flag du tableau).

## Architecture

```
Navigateur (UI)
    ↕ HTTP + WebSocket
Flask bridge (app.py)
    ↕
Orchestrator event-driven (orchestrator.py)
    ↕ MQTT intents / events
Broker Hyperdrive → Host + Vehicles Anki
```

- Pas de couplage dur véhicule ↔ UI
- Subscriptions MQTT créées dynamiquement à la découverte / sélection
- Intents publiés sur `Anki/Hosts/U/{host}/I/{caller}` et `Anki/Vehicles/U/{id}/I/{caller}`

## Docs utiles sur le lab

- API : `http://10.42.0.1:9001`
- Web : `http://10.42.0.1:18443`
