# GCP Project ID
project_id = "voxchain-unlu"

# Region
region = "southamerica-east1"

# Cluster name
cluster_name = "voxchain"

# Repo de GitHub que puede autenticarse por Workload Identity Federation.
# OJO: el default de variables.tf apunta al repo original del que se forkeó
# (MattZander24/...). Si no se pisa acá, la condición del WIF rechaza los tokens
# de este fork y los pipelines 02/03/04 fallan al autenticar contra GCP.
github_repository = "GustavoContardi/TPIntegrador_SDyPP"

# Infra node pool
infra_machine_type = "e2-standard-2"
infra_min_nodes    = 1
infra_max_nodes    = 2

# Apps node pool
apps_machine_type = "e2-standard-2"
apps_min_nodes    = 2
apps_max_nodes    = 3
