variable "project_id" {
  description = "The GCP Project ID where resources will be provisioned."
  type        = string
}

variable "region" {
  description = "The GCP region for Cloud Run and regional resources."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "The name of the Cloud Run service."
  type        = string
  default     = "cxas-zendesk-connector"
}

variable "container_image" {
  description = "Container image URI for Cloud Run (e.g. gcr.io or pkg.dev). If empty, can be deployed via source/Cloud Build."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
}

variable "app_brand" {
  description = "Brand name displayed in the chat interface."
  type        = string
  default     = "Acme Support"
}

variable "zendesk_mock_mode" {
  description = "Whether to run in local/mock specialist simulator mode."
  type        = bool
  default     = false
}

variable "zendesk_subdomain" {
  description = "Zendesk subdomain (e.g. 'company' for company.zendesk.com)."
  type        = string
  default     = ""
}

variable "zendesk_email" {
  description = "Zendesk agent/admin email address."
  type        = string
  default     = ""
}

variable "zendesk_api_token" {
  description = "Zendesk API token. Stored in Secret Manager."
  type        = string
  sensitive   = true
  default     = ""
}
