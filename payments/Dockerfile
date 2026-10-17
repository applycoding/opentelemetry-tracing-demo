FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/payments .

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /out/payments /payments
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/payments"]
