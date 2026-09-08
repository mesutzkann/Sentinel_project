using System.Linq.Expressions;
using FluentValidation;
using MediatR;
using Microsoft.EntityFrameworkCore;
using Sentinel.Application.Common;
using Sentinel.Domain;
using Sentinel.Domain.Entities;

namespace Sentinel.Application.Features.Incidents;

public sealed record IncidentDto(
    Guid Id,
    string IncidentCode,
    Guid ServiceId,
    string ServiceName,
    string Title,
    string? Description,
    IncidentSeverity Severity,
    IncidentStatus Status,
    DateTimeOffset StartedAt,
    DateTimeOffset? ResolvedAt,
    string? CreatedByUsername,
    DateTimeOffset CreatedAt,
    int InvestigationCount);

// ------------------------------------------------------------------------ queries ----

/// <param name="ServiceId">Restrict to one service. Null means all services.</param>
/// <param name="Status">Restrict to one status. Null means all statuses.</param>
/// <param name="ActiveOnly">Exclude resolved and closed incidents. The dashboard's default view.</param>
public sealed record ListIncidentsQuery(
    Guid? ServiceId = null,
    IncidentStatus? Status = null,
    bool ActiveOnly = false,
    int Limit = 50) : IRequest<IReadOnlyList<IncidentDto>>;

public sealed class ListIncidentsHandler
    : IRequestHandler<ListIncidentsQuery, IReadOnlyList<IncidentDto>>
{
    private readonly ISentinelDbContext _db;

    public ListIncidentsHandler(ISentinelDbContext db) => _db = db;

    public async Task<IReadOnlyList<IncidentDto>> Handle(
        ListIncidentsQuery request,
        CancellationToken cancellationToken)
    {
        var query = _db.Incidents.AsNoTracking();

        if (request.ServiceId is { } serviceId)
        {
            query = query.Where(i => i.ServiceId == serviceId);
        }

        if (request.Status is { } status)
        {
            query = query.Where(i => i.Status == status);
        }

        if (request.ActiveOnly)
        {
            query = query.Where(i =>
                i.Status != IncidentStatus.Resolved && i.Status != IncidentStatus.Closed);
        }

        return await query
            .OrderByDescending(i => i.StartedAt)
            .Take(Math.Clamp(request.Limit, 1, 200))
            .Select(Projection)
            .ToListAsync(cancellationToken);
    }

    /// <summary>
    /// Shared projection, so list and detail cannot drift apart.
    /// </summary>
    /// <remarks>
    /// An <see cref="Expression"/> rather than a method: EF Core has to translate this into SQL,
    /// and it cannot see inside a compiled method call.
    /// </remarks>
    internal static readonly Expression<Func<Incident, IncidentDto>> Projection = i => new IncidentDto(
        i.Id,
        i.IncidentCode,
        i.ServiceId,
        i.Service!.Name,
        i.Title,
        i.Description,
        i.Severity,
        i.Status,
        i.StartedAt,
        i.ResolvedAt,
        i.Creator!.Username,
        i.CreatedAt,
        i.Investigations.Count);
}

public sealed record GetIncidentQuery(Guid Id) : IRequest<IncidentDto>;

public sealed class GetIncidentHandler : IRequestHandler<GetIncidentQuery, IncidentDto>
{
    private readonly ISentinelDbContext _db;

    public GetIncidentHandler(ISentinelDbContext db) => _db = db;

    public async Task<IncidentDto> Handle(GetIncidentQuery request, CancellationToken cancellationToken)
    {
        var incident = await _db.Incidents
            .AsNoTracking()
            .Where(i => i.Id == request.Id)
            .Select(ListIncidentsHandler.Projection)
            .FirstOrDefaultAsync(cancellationToken);

        return incident ?? throw new NotFoundException(nameof(Incident), request.Id);
    }
}

// ----------------------------------------------------------------------- commands ----

public sealed record CreateIncidentCommand(
    Guid ServiceId,
    string Title,
    string? Description,
    IncidentSeverity Severity,
    DateTimeOffset? StartedAt) : IRequest<IncidentDto>;

public sealed class CreateIncidentValidator : AbstractValidator<CreateIncidentCommand>
{
    public CreateIncidentValidator()
    {
        RuleFor(c => c.ServiceId).NotEmpty();
        RuleFor(c => c.Title).NotEmpty().MaximumLength(300);
        RuleFor(c => c.Description).MaximumLength(4000);
        RuleFor(c => c.Severity).IsInEnum();

        // A future onset would break deployment correlation, which compares incident start
        // against commit timestamps.
        RuleFor(c => c.StartedAt)
            .LessThanOrEqualTo(_ => DateTimeOffset.UtcNow.AddMinutes(1))
            .When(c => c.StartedAt.HasValue)
            .WithMessage("StartedAt cannot be in the future.");
    }
}

public sealed class CreateIncidentHandler : IRequestHandler<CreateIncidentCommand, IncidentDto>
{
    private readonly ISentinelDbContext _db;
    private readonly ICurrentUser _currentUser;

    public CreateIncidentHandler(ISentinelDbContext db, ICurrentUser currentUser)
    {
        _db = db;
        _currentUser = currentUser;
    }

    public async Task<IncidentDto> Handle(CreateIncidentCommand request, CancellationToken cancellationToken)
    {
        var service = await _db.Services
            .FirstOrDefaultAsync(s => s.Id == request.ServiceId, cancellationToken)
            ?? throw new NotFoundException(nameof(Service), request.ServiceId);

        var incident = new Incident
        {
            ServiceId = service.Id,
            Title = request.Title,
            Description = request.Description,
            Severity = request.Severity,
            StartedAt = request.StartedAt ?? DateTimeOffset.UtcNow,
            CreatedBy = _currentUser.UserId,
        };

        _db.Incidents.Add(incident);
        await _db.SaveChangesAsync(cancellationToken);

        // incident_code comes from a database default, so the in-memory instance does not have
        // it until it is read back.
        await _db.ReloadAsync(incident, cancellationToken);

        return new IncidentDto(
            incident.Id,
            incident.IncidentCode,
            service.Id,
            service.Name,
            incident.Title,
            incident.Description,
            incident.Severity,
            incident.Status,
            incident.StartedAt,
            incident.ResolvedAt,
            _currentUser.Username,
            incident.CreatedAt,
            0);
    }
}

public sealed record UpdateIncidentStatusCommand(Guid Id, IncidentStatus Status) : IRequest<IncidentDto>;

public sealed class UpdateIncidentStatusValidator : AbstractValidator<UpdateIncidentStatusCommand>
{
    public UpdateIncidentStatusValidator()
    {
        RuleFor(c => c.Id).NotEmpty();
        RuleFor(c => c.Status).IsInEnum();
    }
}

public sealed class UpdateIncidentStatusHandler
    : IRequestHandler<UpdateIncidentStatusCommand, IncidentDto>
{
    private readonly ISentinelDbContext _db;

    public UpdateIncidentStatusHandler(ISentinelDbContext db) => _db = db;

    public async Task<IncidentDto> Handle(
        UpdateIncidentStatusCommand request,
        CancellationToken cancellationToken)
    {
        var incident = await _db.Incidents
            .Include(i => i.Service)
            .Include(i => i.Creator)
            .FirstOrDefaultAsync(i => i.Id == request.Id, cancellationToken)
            ?? throw new NotFoundException(nameof(Incident), request.Id);

        incident.Status = request.Status;

        // Resolution time is what the postmortem reports as time-to-resolve, so it is stamped
        // when the status reaches Resolved and cleared if the incident is reopened.
        incident.ResolvedAt = request.Status is IncidentStatus.Resolved or IncidentStatus.Closed
            ? incident.ResolvedAt ?? DateTimeOffset.UtcNow
            : null;

        await _db.SaveChangesAsync(cancellationToken);

        var investigationCount = await _db.Investigations
            .CountAsync(i => i.IncidentId == incident.Id, cancellationToken);

        return new IncidentDto(
            incident.Id,
            incident.IncidentCode,
            incident.ServiceId,
            incident.Service!.Name,
            incident.Title,
            incident.Description,
            incident.Severity,
            incident.Status,
            incident.StartedAt,
            incident.ResolvedAt,
            incident.Creator?.Username,
            incident.CreatedAt,
            investigationCount);
    }
}
