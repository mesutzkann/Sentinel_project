# Build context is ./sample-services, matching the other services.
FROM mcr.microsoft.com/dotnet/sdk:8.0-alpine AS build
WORKDIR /src

COPY delivery-provider/Sentinel.Samples.DeliveryProvider.csproj delivery-provider/
RUN dotnet restore delivery-provider/Sentinel.Samples.DeliveryProvider.csproj

COPY delivery-provider/ delivery-provider/
RUN dotnet publish delivery-provider/Sentinel.Samples.DeliveryProvider.csproj \
    -c Release -o /app/publish --no-restore /p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/aspnet:8.0-alpine AS runtime
WORKDIR /app
USER app

COPY --from=build /app/publish .

EXPOSE 8080
ENTRYPOINT ["dotnet", "Sentinel.Samples.DeliveryProvider.dll"]
