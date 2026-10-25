# ai-models-catalog

Liste de modèles IA pré-filtrée, partagée par les plugins Jeedom `ai_assistant`, `ai_auth2api` et `code_assistant`.

Les plugins livrent une copie de `catalog.json` et peuvent télécharger la version la plus récente sans attendre une mise à jour :

```
https://raw.githubusercontent.com/limad/ai-models-catalog/main/catalog.json
https://raw.githubusercontent.com/limad/ai-models-catalog/main/catalog.json.sha256
```

## Fonctionnement

Chaque lundi, une GitHub Action télécharge [models.dev](https://models.dev/api.json), applique les filtres ci-dessous, valide le résultat contre `schema/catalog.schema.json` et commite `catalog.json` s'il a changé.

Filtres (dans l'ordre) :

1. fournisseurs listés dans `config/providers.json` seulement ;
2. exclusion des modèles hors chat (embeddings, image, vidéo, audio, transcription, temps réel), obsolètes, sans appel d'outils, ou spécialisés (deep-research, computer-use, guard…) ;
3. suppression des snapshots datés quand l'alias existe, et des alias `-latest` qui doublonnent une version ;
4. par famille, les 2 dates de sortie les plus récentes (1 pour OpenRouter, réglable par fournisseur avec `per_family`), et rien de plus vieux que 18 mois ;
5. corrections manuelles de `config/overrides.json` : `pin` (toujours gardé, marqué `recommended`), `exclude`, libellés, ajouts de modèles absents de models.dev.

**Libellés sans prix :** un prix dans `label` ou `name` fait échouer la génération ; le prix vit dans `cost`.

**Garde-fou :** si un fournisseur se retrouve vide, si son `default_model` disparaît, ou si plus de 30 % des modèles disparaissent d'un coup, l'Action échoue et rien n'est publié.

## En local

```bash
python3 scripts/build.py --report        # télécharge models.dev, affiche les exclusions
python3 scripts/build.py --source api.json  # depuis une copie locale
```

Le choix des modèles exposés reste côté plugin (configuration de l'utilisateur) : ce dépôt ne fournit que la base commune.

## Licence

MIT. Données dérivées de [models.dev](https://github.com/sst/models.dev) (MIT, © 2025 models.dev).
