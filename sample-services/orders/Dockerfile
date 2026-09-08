# Build context is ./sample-services, so the shared project is reachable.
FROM mcr.microsoft.com/dotnet/sdk:8.0-alpine AS build
WORKDIR /src

# Restore first, with only the project files copied, so a code change does not
# invalidate the restore layer.
COPY _shared/Sentinel.Samples.Common/Sentinel.Samples.Common.csproj _shared/Sentinel.Samples.Common/
COPY orders/Sentinel.Samples.Orders.csproj orders/
RUN dotnet restore orders/Sentinel.Samples.Orders.csproj

COPY _shared/ _shared/
COPY orders/ orders/
RUN dotnet publish orders/Sentinel.Samples.Orders.csproj \
    -c Release -o /app/publish --no-restore /p:UseAppHost=false

# aspnet:8.0-alpine is ~110 MB against ~220 MB for the Debian image. With five of these
# running alongside the observability stack on a 16 GB laptop, that difference matters.
FROM mcr.microsoft.com/dotnet/aspnet:8.0-alpine AS runtime
WORKDIR /app

# The .NET 8 runtime images already ship a non-root `app` user and export its id as
# APP_UID, so creating one here only collides with it.
USER $APP_UID

COPY --from=build /app/publish .

EXPOSE 8080
ENTRYPOINT ["dotnet", "Sentinel.Samples.Orders.dll"]
