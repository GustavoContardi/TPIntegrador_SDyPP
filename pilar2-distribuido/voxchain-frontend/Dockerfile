# Build stage
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

# Production stage
# nginx-unprivileged: corre como uid 101 (no-root), escucha en 8080
FROM nginxinc/nginx-unprivileged:alpine

# Copy the built Angular app to nginx
COPY --from=build /app/dist/voxchain-frontend/browser /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
