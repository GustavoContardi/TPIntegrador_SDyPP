# Import de los recursos de WIF que sobrevivieron al destroy del 2026-07-14.
#
# El pool y el provider quedaron soft-deleted 30 días; se les hizo `undelete`
# el 2026-08-04 (antes de que expiraran el 13/08) en vez de recrearlos con otro
# nombre, para que el secret GCP_WIF_PROVIDER ya cargado en GitHub siga siendo
# válido. Como ya existen en GCP, hay que importarlos o el apply falla con
# "already exists".
#
# Se usan bloques `import` y no `tofu import` en línea porque los providers
# kubernetes/helm se configuran desde el endpoint del clúster, que no existe
# hasta el apply, y eso bloquea el comando de import.
#
# Una vez aplicado, este archivo se puede borrar.

import {
  to = google_iam_workload_identity_pool.github
  id = "projects/voxchain-unlu/locations/global/workloadIdentityPools/github-actions"
}

import {
  to = google_iam_workload_identity_pool_provider.github
  id = "projects/voxchain-unlu/locations/global/workloadIdentityPools/github-actions/providers/github-provider"
}
