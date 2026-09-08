using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Sentinel.Application.Common;
using Sentinel.Domain.Entities;

namespace Sentinel.Application.Features.Services;

public sealed record ServiceDto(
    Guid Id,
    string Name,
    string DisplayName,
    string? RepoPath,
    string? HealthUrl,
    string? MetricsJob,
    int OpenIncidentCount,
    DateTimeOffset CreatedAt);

// ------------------------------------------------------------------------ queries ----

public sealed record ListServicesQuery : IRequest<IReadOnlyList<ServiceDto>>;

public sealed class ListServicesHandler
    : IRequestHandler<ListServicesQuery, IReadOnlyList<ServiceDto>>
{
    private readonly ISentinelDbContext _db;

    public ListServicesHandler(ISentinelDbContext db) => _db = db;

    public async Task<IReadOnlyList<ServiceDto>> Handle(
        ListServicesQuery request,
        CancellationToken cancellationToken)
    {
        // The open-incident count is projected in SQL rather than loaded and counted in memory:
        // the dashboard calls this on every poll.
        return await _db.Services
            .AsNoTracking()
            .OrderBy(s => s.Name)
            .Select(s => new ServiceDto(
                s.Id,
                s.Name,
                s.DisplayName,
                s.RepoPath,
                s.HealthUrl,
                s.MetricsJob,
                s.Incidents.Count(i =>
                    i.Status != Domain.IncidentStatus.Resolved
                    && i.Status != Domain.IncidentStatus.Closed),
                s.CreatedAt))
            .ToListAsync(cancellationToken);
    }
}

public sealed record GetServiceQuery(Guid Id) : IRequest<ServiceDto>;

public sealed class GetServiceHandler : IRequestHandler<GetServiceQuery, ServiceDto>
{
    private readonly ISentinelDbContext _db;

    public GetServiceHandler(ISentinelDbContext db) => _db = db;

    public async Task<ServiceDto> Handle(GetServiceQuery request, CancellationToken cancellationToken)
    {
        var service = await _db.Services
            .AsNoTracking()
            .Where(s => s.Id == request.Id)
            .Select(s => new ServiceDto(
                s.Id,
                s.Name,
                s.DisplayName,
                s.RepoPath,
                s.HealthUrl,
                s.MetricsJob,
                s.Incidents.Count(i =>
                    i.Status != Domain.IncidentStatus.Resolved
                    && i.Status != Domain.IncidentStatus.Closed),
                s.CreatedAt))
            .FirstOrDefaultAsync(cancellationToken);

        return service ?? throw new NotFoundException(nameof(Service), request.Id);
    }
}

// ----------------------------------------------------------------------- commands ----

public sealed record CreateServiceCommand(
    string Name,
    string DisplayName,
    string? RepoPath,
    string? HealthUrl,
    string? MetricsJob) : IRequest<ServiceDto>;

public sealed class CreateServiceValidator : AbstractValidator<CreateServiceCommand>
{
    public CreateServiceValidator()
    {
        // Lowercase with hyphens, matching the OTel service.name convention the agent joins on.
        RuleFor(c => c.Name)
            .NotEmpty()
            .MaximumLength(100)
            .Matches("^[a-z0-9][a-z0-9-]*$")
            .WithMessage("Name must be lowercase letters, digits and hyphens, e.g. 'payment-service'.");

        RuleFor(c => c.DisplayName).NotEmpty().MaximumLength(200);
        RuleFor(c => c.RepoPath).MaximumLength(500);
        RuleFor(c => c.HealthUrl).MaximumLength(500);
        RuleFor(c => c.MetricsJob).MaximumLength(100);
    }
}

public sealed class CreateServiceHandler : IRequestHandler<CreateServiceCommand, ServiceDto>
{
    private readonly ISentinelDbContext _db;

    public CreateServiceHandler(ISentinelDbContext db) => _db = db;

    public async Task<ServiceDto> Handle(CreateServiceCommand request, CancellationToken cancellationToken)
    {
        if (await _db.Services.AnyAsync(s => s.Name == request.Name, cancellationToken))
        {
            throw new ConflictException($"A service named '{request.Name}' already exists.");
        }

        var service = new Service
        {
            Name = request.Name,
            DisplayName = request.DisplayName,
            RepoPath = request.RepoPath,
            HealthUrl = request.HealthUrl,
            MetricsJob = request.MetricsJob,
        };

        _db.Services.Add(service);
        await _db.SaveChangesAsync(cancellationToken);

        return new ServiceDto(
            service.Id,
            service.Name,
            service.DisplayName,
            service.RepoPath,
            service.HealthUrl,
            service.MetricsJob,
            0,
            service.CreatedAt);
    }
}

public sealed record UpdateServiceCommand(
    Guid Id,
    string DisplayName,
    string? RepoPath,
    string? HealthUrl,
    string? MetricsJob) : IRequest<ServiceDto>;

public sealed class UpdateServiceValidator : AbstractValidator<UpdateServiceCommand>
{
    public UpdateServiceValidator()
    {
        RuleFor(c => c.DisplayName).NotEmpty().MaximumLength(200);
        RuleFor(c => c.RepoPath).MaximumLength(500);
        RuleFor(c => c.HealthUrl).MaximumLength(500);
        RuleFor(c => c.MetricsJob).MaximumLength(100);
    }
}

public sealed class UpdateServiceHandler : IRequestHandler<UpdateServiceCommand, ServiceDto>
{
    private readonly ISentinelDbContext _db;

    public UpdateServiceHandler(ISentinelDbContext db) => _db = db;

    public async Task<ServiceDto> Handle(UpdateServiceCommand request, CancellationToken cancellationToken)
    {
        var service = await _db.Services.FirstOrDefaultAsync(s => s.Id == request.Id, cancellationToken)
            ?? throw new NotFoundException(nameof(Service), request.Id);

        // Name is intentionally not updatable: it is the correlation key across Loki, Prometheus
        // and Jaeger, and renaming it would orphan every historical signal.
        service.DisplayName = request.DisplayName;
        service.RepoPath = request.RepoPath;
        service.HealthUrl = request.HealthUrl;
        service.MetricsJob = request.MetricsJob;

        await _db.SaveChangesAsync(cancellationToken);

        var openIncidents = await _db.Incidents.CountAsync(
            i => i.ServiceId == service.Id
                 && i.Status != Domain.IncidentStatus.Resolved
                 && i.Status != Domain.IncidentStatus.Closed,
            cancellationToken);

        return new ServiceDto(
            service.Id,
            service.Name,
            service.DisplayName,
            service.RepoPath,
            service.HealthUrl,
            service.MetricsJob,
            openIncidents,
            service.CreatedAt);
    }
}

public sealed record DeleteServiceCommand(Guid Id) : IRequest;

public sealed class DeleteServiceHandler : IRequestHandler<DeleteServiceCommand>
{
    private readonly ISentinelDbContext _db;

    public DeleteServiceHandler(ISentinelDbContext db) => _db = db;

    public async Task Handle(DeleteServiceCommand request, CancellationToken cancellationToken)
    {
        var service = await _db.Services.FirstOrDefaultAsync(s => s.Id == request.Id, cancellationToken)
            ?? throw new NotFoundException(nameof(Service), request.Id);

        // Incidents reference the service with DeleteBehavior.Restrict. Checking here turns what
        // would be an opaque database error into an explanation.
        if (await _db.Incidents.AnyAsync(i => i.ServiceId == request.Id, cancellationToken))
        {
            throw new ConflictException(
                $"Service '{service.Name}' still has incidents and cannot be deleted.");
        }

        _db.Services.Remove(service);
        await _db.SaveChangesAsync(cancellationToken);
    }
}
