namespace Sentinel.Domain.Entities;

/// <summary>
/// Something went wrong with a service. The unit of work the whole platform is organised around:
/// an incident is investigated, gets a root cause, gets remediated, and ends as a postmortem
/// that feeds back into retrieval.
/// </summary>
public sealed class Incident
{
    public Guid Id { get; set; } = Guid.NewGuid();

    /// <summary>
    /// Human-facing identifier, <c>INC-00042</c>. Assigned by
    /// <c>sentinel.next_incident_code()</c> so concurrent inserts cannot collide.
    /// </summary>
    /// <remarks>
    /// Left at the CLR default rather than <c>string.Empty</c>: EF omits a generated column from
    /// the INSERT only when the property still holds that default, and an empty string would be
    /// written instead of the sequence value — which the unique index then rejects on the second
    /// incident. EF populates this from the RETURNING clause once the row is inserted.
    /// </remarks>
    public string IncidentCode { get; set; } = null!;

    public Guid ServiceId { get; set; }

    public Service? Service { get; set; }

    public required string Title { get; set; }

    public string? Description { get; set; }

    public IncidentSeverity Severity { get; set; } = IncidentSeverity.Medium;

    public IncidentStatus Status { get; set; } = IncidentStatus.Open;

    /// <summary>
    /// When the failure began, not when the record was created. The agent correlates this with
    /// deployment timestamps, so it has to be the real onset.
    /// </summary>
    public DateTimeOffset StartedAt { get; set; } = DateTimeOffset.UtcNow;

    public DateTimeOffset? ResolvedAt { get; set; }

    public Guid? CreatedBy { get; set; }

    public User? Creator { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;

    public List<Investigation> Investigations { get; set; } = [];

    public List<Postmortem> Postmortems { get; set; } = [];
}
