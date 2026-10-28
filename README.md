# signaux-btc

Signaux intraday sur un FNB Bitcoin coté en CAD au TSX (par défaut BTCC.B), envoyés sur le téléphone avec [ntfy](https://ntfy.sh).
Le script ne passe aucun ordre : il propose, et tu exécutes toi-même dans Wealthsimple.

- `signals.py live` : un passage, lancé toutes les 5 min pendant la séance par `.github/workflows/signaux.yml`.
- `signals.py backtest` : rejoue les ~60 derniers jours de bougies 5 min (BTCC.B et FBTC), frais inclus. Les résultats apparaissent dans le résumé du workflow « Backtest ».
- `config.json` : tous les réglages (stop, objectifs, nombre de trades par jour, seuil de pause). `alerts_enabled` doit passer à `true` pour activer les alertes.
- `state/state.json` : compte estimé et journal des trades, mis à jour par le workflow.

Stratégie (mode agressif, 100 % du compte par trade, achat seulement) :
1. Range d'ouverture = les 30 premières minutes.
2. Achat sur une cassure du haut du range, au-dessus du VWAP, avec du volume, ou sur une reprise du VWAP après 10 h 30.
3. Stop à -1,25 %. Objectif 1 à +3 % (on vend la moitié et on remonte le stop au prix d'entrée), objectif 2 à +5 %.
4. Vente obligatoire à 15 h 45. Maximum 4 entrées par jour. Pause si le compte estimé tombe sous 50 $.

Aucun résultat n'est garanti. Le backtest porte sur peu de données et ne prédit pas l'avenir.
