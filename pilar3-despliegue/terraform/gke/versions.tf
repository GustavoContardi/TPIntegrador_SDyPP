terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.6"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.6"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.31"
    }
  }

  # Estado remoto: sin esto el runner de 01-infra arranca con el estado vacío y
  # un plan propone recrear todo. El bucket no lo crea este código (tendría que
  # existir antes de su propio `init`): lo crea bootstrap-secrets.sh.
  backend "gcs" {
    bucket = "voxchain-unlu-tfstate"
    prefix = "gke"
  }
}
