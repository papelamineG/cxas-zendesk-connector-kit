terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 1. Enable Required Google Cloud Services
locals {
  services = [
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "ces.googleapis.com"
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.services)
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

# 2. Secret Manager Secret for Zendesk API Token
resource "google_secret_manager_secret" "zendesk_token" {
  count     = var.zendesk_api_token != "" ? 1 : 0
  project   = var.project_id
  secret_id = "zendesk-api-token"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "zendesk_token_version" {
  count       = var.zendesk_api_token != "" ? 1 : 0
  secret      = google_secret_manager_secret.zendesk_token[0].id
  secret_data = var.zendesk_api_token
}

# 3. Dedicated Service Account for Cloud Run
resource "google_service_account" "cloud_run_sa" {
  project      = var.project_id
  account_id   = "cxas-zendesk-runner"
  display_name = "CXAS Zendesk Connector Cloud Run Service Account"
}

# Grant Secret Manager Secret Accessor to Cloud Run Service Account
resource "google_secret_manager_secret_iam_member" "token_access" {
  count     = var.zendesk_api_token != "" ? 1 : 0
  project   = var.project_id
  secret_id = google_secret_manager_secret.zendesk_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.cloud_run_sa.email}"
}

# 4. Cloud Run v2 Service
resource "google_cloud_run_v2_service" "connector" {
  project  = var.project_id
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.cloud_run_sa.email

    scaling {
      min_instance_count = 0
      max_instance_count = 1 # Single instance preserves in-memory escalation session state
    }

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "APP_BRAND"
        value = var.app_brand
      }
      env {
        name  = "ZENDESK_MOCK_MODE"
        value = tostring(var.zendesk_mock_mode)
      }
      env {
        name  = "ZENDESK_SUBDOMAIN"
        value = var.zendesk_subdomain
      }
      env {
        name  = "ZENDESK_EMAIL"
        value = var.zendesk_email
      }

      dynamic "env" {
        for_each = var.zendesk_api_token != "" ? [1] : []
        content {
          name = "ZENDESK_API_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.zendesk_token[0].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.token_access
  ]
}

# 5. Public Invoker IAM Policy (Allow Unauthenticated)
resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.connector.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
